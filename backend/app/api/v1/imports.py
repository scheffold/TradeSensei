# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Route handlers for the broker file import endpoint.

Accepts multipart file uploads and delegates to
:class:`~app.services.import_service.ImportService` for parsing and
persisting trades.  Also exposes an IBKR Flex Query automatic import
endpoint backed by :class:`~app.services.flex_import_service.FlexImportService`.
"""

import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from fastapi import Form as FastAPIForm
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import GENERIC_ACCOUNT_API_ERROR, GENERIC_IMPORT_MAX_ERRORS, IMPORT_MAX_FILE_SIZE_MB
from app.core.dependencies import require_portfolio_owner
from app.core.deps import get_current_user, get_session_with_rls
from app.core.exceptions import (
    AccountNotFoundError,
    BrokerRawTransactionNotFoundError,
    FlexAPIError,
    GenericImportValidationError,
    ImportAccountMismatchError,
    ImportNotAllowedError,
    ImportParseError,
    RateLimitError,
    UnsupportedBrokerError,
)
from app.models.account import AccountType
from app.models.user import User
from app.repositories.account_repository import AccountRepository
from app.schemas.broker_raw_transaction import BrokerRawTransactionRead
from app.schemas.common import ResponseEnvelope
from app.schemas.imports import FlexImportRequest, FreshStartResult, ImportBadRequestResponse, ImportResult
from app.services.account_service import AccountService
from app.services.flex_import_service import FlexImportService, is_transient_send_request_error
from app.services.import_service import ImportService
from app.utils.parsers.factory import BrokerType

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/portfolios/{portfolio_id}/imports",
    tags=["imports"],
    dependencies=[Depends(require_portfolio_owner)],
)

# 1 MB in bytes.
_BYTES_PER_MB: int = 1024 * 1024

# Allowed file extensions per broker type.
_BROKER_ALLOWED_EXTENSIONS: dict[BrokerType, frozenset[str]] = {
    BrokerType.IBKR: frozenset({"xml"}),
    BrokerType.SCHWAB: frozenset({"json"}),
    BrokerType.GENERIC: frozenset({"csv", "json"}),
}

# Allowed broker types per account type (manual accounts cannot import files).
_ACCOUNT_TYPE_BROKER: dict[AccountType, BrokerType] = {
    AccountType.IBKR: BrokerType.IBKR,
    AccountType.SCHWAB: BrokerType.SCHWAB,
    AccountType.GENERIC: BrokerType.GENERIC,
}


def _get_service(
    session: Annotated[AsyncSession, Depends(get_session_with_rls)],
) -> ImportService:
    """FastAPI dependency that constructs an :class:`ImportService`.

    Args:
        session: Injected async database session.

    Returns:
        An :class:`ImportService` bound to the current session.
    """
    return ImportService(session)


def _get_account_service(
    session: Annotated[AsyncSession, Depends(get_session_with_rls)],
) -> AccountService:
    """Inject account ownership/metadata access without exposing a session."""
    return AccountService(session)


def _generic_validation_detail(exc: GenericImportValidationError) -> dict[str, object]:
    """Cap HTTP row errors while preserving a safe omitted-error count."""
    visible = exc.errors[:GENERIC_IMPORT_MAX_ERRORS]
    errors = [{"location": error.location, "field": error.field, "message": error.message} for error in visible]
    if len(exc.errors) > GENERIC_IMPORT_MAX_ERRORS:
        omitted = len(exc.errors) - GENERIC_IMPORT_MAX_ERRORS
        errors.append({"location": "document", "field": "errors", "message": f"{omitted} more validation errors omitted."})
    return {"message": str(exc), "errors": errors}


def _get_flex_service() -> FlexImportService:
    """FastAPI dependency that constructs a :class:`FlexImportService`.

    ``FlexImportService`` is a pure HTTP client with no database
    dependency, so this factory takes no injected session.

    Returns:
        A :class:`FlexImportService` bound to the configured Flex token.

    Raises:
        HTTPException: 503 if ``IBKR_FLEX_TOKEN`` is not configured.
    """
    token = settings.ibkr_flex_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("IBKR Flex import is not configured on this server. Set the IBKR_FLEX_TOKEN environment variable to enable it."),
        )
    return FlexImportService(flex_token=token)


async def _validate_account_import(
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    user_id: uuid.UUID,
    extension: str,
    service: AccountService,
) -> BrokerType:
    """Verify the account exists, is not manual, and the file extension matches.

    Args:
        account_id: UUID of the target account.
        portfolio_id: UUID of the owned portfolio containing the account.
        user_id: UUID of the authenticated user.
        extension: Lowercase file extension (without leading dot).
        service: Injected service for the account ownership lookup.

    Returns:
        The :class:`BrokerType` to use for parsing.

    Raises:
        ImportNotAllowedError: If the account is manual, or the file extension
            does not match the expected format for the account type.
        AccountNotFoundError: If the account does not exist or is not owned by
            the user.
    """
    try:
        account = await service.get_account(account_id, user_id, portfolio_id)
    except AccountNotFoundError:
        raise AccountNotFoundError(f"Account {account_id} not found for user {user_id}.") from None
    broker = _ACCOUNT_TYPE_BROKER.get(account.type)
    if broker is None:
        raise ImportNotAllowedError("File import is not supported for manual accounts")
    allowed = _BROKER_ALLOWED_EXTENSIONS[broker]
    if extension not in allowed:
        if account.type == AccountType.SCHWAB and extension == "csv":
            raise ImportNotAllowedError("Schwab accounts require JSON format (.json). CSV import is no longer supported.")
        raise ImportNotAllowedError(
            f"Account '{account.name}' ({account.type.upper()}) only accepts {', '.join(f'.{e}' for e in sorted(allowed))} files. Received: .{extension}"
        )
    return broker


@router.post(
    "/upload",
    response_model=ResponseEnvelope[ImportResult],
    status_code=status.HTTP_200_OK,
    summary="Upload a broker export file and import trades",
    description=(
        "Parse a broker export file and bulk-create the contained trades for "
        "the specified account. The accepted file format is determined by the "
        "account type: IBKR accounts accept XML (Flex Query), Schwab accounts "
        "accept JSON, and generic accounts accept generic v1 CSV/JSON. "
        "Manual accounts do not support file import. The account must belong to the owned portfolio in the path. "
        "Generic files contain one fill per record, not round-trip trades. UTF-8 CSV (optional BOM) "
        "and versioned JSON are detected from content after the extension check; XML content is not supported. "
        "Generic instrument_id is required for FUTURE, plain-symbol OPTION and every order_id-linked fill, even a single OCC leg. "
        "Without order_id, ordinary EQUITY/FOREX/CRYPTO and valid OCC-symbol options may omit it. "
        "Keep symbol spelling and ID presence/values consistent across exports; explicit option fields must agree with any valid OCC symbol. "
        "Every order_id requires explicit OPEN/CLOSE; order-linked CLOSE also requires reported realized_pnl (genuine zero is valid). "
        "OPEN must not report non-null realized_pnl, including zero. Order markers must agree within the file and with persisted "
        "target-account raw history, checked under the account lock before persistence. "
        "Only ordinary CLOSE without order metadata uses the zero-marker fallback. "
        "Execution decimals and split ratios must fit Numeric(18,8), and executed_at must be timezone-aware and representable in UTC. "
        "Original execution records and unknown fields remain inert in raw_data.source_record; only validated fields drive active order metadata and cash. "
        "Generic validation is all-or-nothing: HTTP 400 detail is an object with message and safe location/field/message errors. "
        f"At most {GENERIC_IMPORT_MAX_ERRORS} row errors are returned, plus a final omitted-count marker when truncated. "
        "Other parse and processing errors retain string detail. Generic execution IDs are scoped to the user and account; "
        "re-imports skip existing IDs without updating their stored raw fills. "
        "Protection/fallback matching uses account_id, symbol, asset_class, broker_conid and opened_at, with execution anchors taking precedence. "
        "Different null-ID instruments at the same timestamp do not share protection, nor do trades in different accounts. "
        "Authenticated reference downloads are available at /api/v1/imports/generic/schema and /api/v1/imports/generic/template.csv. "
        f"Maximum file size: {IMPORT_MAX_FILE_SIZE_MB} MB."
    ),
    response_description=("A summary of the import operation wrapped in a response envelope, including the number of trades created and any parse warnings."),
    responses={
        200: {"description": "Import completed. Check warnings for non-fatal issues, including ignored generic CSV columns."},
        400: {
            "model": ImportBadRequestResponse,
            "description": (
                "Parse/processing failure with legacy string detail, or generic validation failure with structured detail.message/detail.errors. "
                f"The error list contains at most {GENERIC_IMPORT_MAX_ERRORS} row errors plus one omitted-count marker. "
                "Includes derivative/OCC/order/P&L/numeric/UTC errors and conflicts with existing target-account order markers. "
                "Generic validation saves no raw transactions, executions or trades; row errors never echo supplied values."
            ),
        },
        401: {"description": "Authentication required."},
        404: {"description": "Portfolio or account not found, not owned by the current user, or account outside the path portfolio."},
        413: {"description": "File exceeds the maximum allowed size."},
        422: {"description": "Invalid request fields, file type not allowed for this account type, manual account, or Schwab account mismatch/ambiguity."},
    },
)
async def upload_import(
    portfolio_id: uuid.UUID,
    service: Annotated[ImportService, Depends(_get_service)],
    account_service: Annotated[AccountService, Depends(_get_account_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[
        UploadFile,
        File(description="Broker file: IBKR .xml, Schwab .json, or generic v1 .csv/.json. Generic content is UTF-8 with one fill per record."),
    ],
    account_id: Annotated[
        uuid.UUID,
        FastAPIForm(
            description="UUID of the target account in the owned path portfolio; only a generic account accepts generic v1 CSV/JSON.",
            examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
        ),
    ],
    overwrite_manually_edited: Annotated[
        bool,
        FastAPIForm(
            description=(
                "When true, trades that have been manually edited are no longer "
                "protected and will be overwritten by the incoming import data. "
                "Defaults to false."
            ),
            examples=[False, True],
        ),
    ] = False,
) -> ResponseEnvelope[ImportResult]:
    """Upload a broker export file and import the trades it contains.

    The broker parser is determined automatically from the account type:
    IBKR accounts use the XML (Flex Query) parser, Schwab accounts use the
    JSON parser. Generic accounts use the v1 CSV/JSON parser. Manual accounts
    are rejected with HTTP 422.

    Generic validation rejects the whole file before persistence. Its HTTP 400
    detail contains message and errors, capped at 200 row errors plus an
    omitted-count marker. Errors identify CSV lines or JSON array indexes and
    field names, never supplied values. Other HTTP 400 details remain strings.
    Re-importing a generic execution ID in the same account does not update
    the stored raw fill; IDs in different generic accounts do not collide.
    FUTURE, plain-symbol OPTION and every order-linked fill require stable
    instrument IDs. Supplied option fields must agree with valid OCC symbols.
    Order-linked fills require consistent OPEN/CLOSE markers, also checked
    against persisted target-account raw history under the account lock.
    Order-linked CLOSE requires reported P&L, including genuine zero; OPEN
    must not report P&L, even zero. Only CLOSE without order metadata retains
    the zero-marker fallback. Numeric and UTC overflow failures are field errors.
    Original execution data and unknown extras remain inert in source_record.
    Protection/fallback identity includes account, symbol, asset class, contract
    ID and opening timestamp; execution anchors take precedence where available.

    Args:
        portfolio_id: UUID of the owned portfolio containing the account.
        service: Injected import service.
        account_service: Injected service used for the account type lookup.
        current_user: The authenticated user.
        file: IBKR XML, Schwab JSON, or generic v1 CSV/JSON file.
        account_id: UUID of the target account within the path portfolio.
        overwrite_manually_edited: When ``True``, manually-edited trades
            are overwritten instead of skipped.

    Returns:
        A :class:`~app.schemas.common.ResponseEnvelope` wrapping an
        :class:`~app.schemas.imports.ImportResult` with counts and
        any non-fatal warnings.

    Raises:
        HTTPException: 400 if generic validation fails (structured detail),
            including order-marker conflicts with persisted account history,
            or parsing/processing fails (legacy string detail).
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the portfolio or account is not found or not
            owned by the current user, or the account is outside the portfolio.
        HTTPException: 413 if the uploaded file exceeds the size limit.
        HTTPException: 422 if the file type does not match the account type,
            the account is manual, a Schwab account mismatch is detected,
            or request fields fail validation.
    """
    filename = file.filename or "upload"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # --- Validate account type and file extension --------------------------
    try:
        broker = await _validate_account_import(
            account_id=account_id,
            portfolio_id=portfolio_id,
            user_id=current_user.id,
            extension=extension,
            service=account_service,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ImportNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # --- Read file content with size guard ---------------------------------
    max_bytes = IMPORT_MAX_FILE_SIZE_MB * _BYTES_PER_MB
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(f"File exceeds the maximum allowed size of {IMPORT_MAX_FILE_SIZE_MB} MB."),
        )

    # --- Delegate to service -----------------------------------------------
    try:
        result = await service.import_trades(
            content=content,
            filename=filename,
            broker=broker,
            portfolio_id=portfolio_id,
            account_id=account_id,
            user_id=current_user.id,
            overwrite_manually_edited=overwrite_manually_edited,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except GenericImportValidationError as exc:
        logger.warning(
            "generic_import_validation_failed user_id=%s account_id=%s error_count=%d fields=%s",
            current_user.id,
            account_id,
            len(exc.errors),
            ",".join(sorted({error.field for error in exc.errors})),
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_generic_validation_detail(exc)) from None
    except (ImportParseError, UnsupportedBrokerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ImportAccountMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ImportNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except Exception as exc:
        if broker == BrokerType.GENERIC:
            logger.error(
                "generic_import_failed user_id=%s account_id=%s error_type=%s",
                current_user.id,
                account_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to process generic import file.") from None
        logger.exception("Unhandled exception during import_trades")

        if isinstance(exc, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Validation error during import parsing: {exc}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process import file. An unexpected error occurred: {exc}",
        ) from exc

    return ResponseEnvelope[ImportResult](data=result)


@router.post(
    "/{account_id}/fresh-start",
    response_model=ResponseEnvelope[FreshStartResult],
    status_code=status.HTTP_200_OK,
    summary="Reset an account and re-import from scratch",
    description=(
        "Destructive operation — deletes ALL raw transactions, logical executions and trades for the specified account, "
        "including suppressed/deleted history and manually edited trades, then rebuilds from the uploaded file. "
        "Suppression flags on deleted raw history do not survive this reset. The account type is unchanged. "
        "The operation shares the account lock with ingestion and type updates. "
        "The entire operation runs in a single DB transaction — any error rolls "
        "back all changes. "
        "The accepted file format is determined by the account type: IBKR accepts "
        "XML (Flex Query), Schwab accepts JSON, and generic accepts generic v1 CSV/JSON. "
        "Manual accounts cannot import files. Requires acknowledged=true and an account in the owned path portfolio. "
        "Generic files follow the upload endpoint's derivative identity, OCC agreement, explicit order marker/closing P&L and numeric/UTC rules. "
        "Generic all-or-nothing validation errors return HTTP 400 with detail.message and safe detail.errors; "
        f"at most {GENERIC_IMPORT_MAX_ERRORS} row errors are returned, plus an omitted-count marker when truncated. "
        "Missing acknowledgement and other parse/processing failures retain string detail. "
        f"Maximum file size: {IMPORT_MAX_FILE_SIZE_MB} MB."
    ),
    response_description="Reset delete counts and the subsequent import result, wrapped in a response envelope.",
    responses={
        200: {"description": "Fresh-start completed. Check import_result for details."},
        400: {
            "model": ImportBadRequestResponse,
            "description": (
                "Acknowledgement missing/false or parse/processing failure (legacy string detail), "
                "or generic file validation failure (structured detail.message/detail.errors). "
                f"At most {GENERIC_IMPORT_MAX_ERRORS} row errors plus one omitted-count marker are returned. All reset changes roll back on failure."
            ),
        },
        401: {"description": "Authentication required."},
        404: {"description": "Portfolio or account not found, not owned by the current user, or account outside the path portfolio."},
        413: {"description": "File exceeds the maximum allowed size."},
        422: {"description": "Invalid request fields, manual account, or file type not allowed for the account type."},
    },
)
async def fresh_start_import(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    service: Annotated[ImportService, Depends(_get_service)],
    account_service: Annotated[AccountService, Depends(_get_account_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File(description="IBKR .xml, Schwab .json, or generic v1 .csv/.json file used to rebuild the account.")],
    acknowledged: Annotated[
        bool,
        FastAPIForm(
            description="Must be true to delete all raw, logical and trade history, including suppressed/deleted rows, before re-importing.",
            examples=[True],
        ),
    ] = False,
) -> ResponseEnvelope[FreshStartResult]:
    """Reset an account and re-import all trades from a broker file.

    The broker parser is determined automatically from the account type:
    IBKR XML, Schwab JSON, or generic v1 CSV/JSON. Manual accounts are rejected.
    Generic validation returns structured HTTP 400 detail, capped at 200 row
    errors plus an omitted-count marker; other HTTP 400 detail remains a string.
    Invalid input rolls back the reset and import together.
    Raw history, including suppression flags, is deleted along with logical
    executions and trades before rebuilding; account type is unchanged.
    The reset/re-import shares the account lock with ingestion and type updates.
    Generic derivative identity, OCC agreement, order markers/reported closing
    P&L and numeric/UTC validation follow the ordinary upload contract.

    Args:
        portfolio_id: UUID of the owned portfolio containing the account.
        account_id: UUID of the account to reset.
        service: Injected import service.
        account_service: Injected service used for the account type lookup.
        current_user: The authenticated user.
        file: The uploaded IBKR XML, Schwab JSON, or generic v1 CSV/JSON file.
        acknowledged: Must be True to confirm deletion of all account trading
            history, including suppressed/deleted rows, before rebuilding.

    Returns:
        A fresh-start result with delete counts and the import result.

    Raises:
        HTTPException: 400 if generic validation fails (structured detail),
            acknowledgement is false, or parsing/processing fails (string detail).
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the portfolio/account is missing, not owned,
            or the account is outside the path portfolio.
        HTTPException: 413 if the file is too large.
        HTTPException: 422 if request fields are invalid, the account is manual,
            or the file type does not match the account type.
    """
    if not acknowledged:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Please set acknowledged=true to confirm that all trades and executions for this account will be deleted before re-importing."),
        )

    filename = file.filename or "upload"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # --- Validate account type and file extension --------------------------
    try:
        broker = await _validate_account_import(
            account_id=account_id,
            portfolio_id=portfolio_id,
            user_id=current_user.id,
            extension=extension,
            service=account_service,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ImportNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # --- Read file with size guard ---
    max_bytes = IMPORT_MAX_FILE_SIZE_MB * _BYTES_PER_MB
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(f"File exceeds the maximum allowed size of {IMPORT_MAX_FILE_SIZE_MB} MB."),
        )

    # --- Apply statement_timeout guard ---
    timeout_seconds = settings.fresh_start_timeout
    await service.set_statement_timeout(timeout_seconds)

    try:
        result = await service.fresh_start_import(
            content=content,
            filename=filename,
            broker=broker,
            portfolio_id=portfolio_id,
            account_id=account_id,
            user_id=current_user.id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except GenericImportValidationError as exc:
        logger.warning(
            "generic_fresh_start_validation_failed user_id=%s account_id=%s error_count=%d fields=%s",
            current_user.id,
            account_id,
            len(exc.errors),
            ",".join(sorted({error.field for error in exc.errors})),
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_generic_validation_detail(exc)) from None
    except (ImportParseError, UnsupportedBrokerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ImportNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except Exception as exc:
        if broker == BrokerType.GENERIC:
            logger.error(
                "generic_fresh_start_failed user_id=%s account_id=%s error_type=%s",
                current_user.id,
                account_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to process generic import file.") from None
        logger.exception("Unhandled exception during import_trades")

        if isinstance(exc, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Validation error during import parsing: {exc}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process import file. An unexpected error occurred: {exc}",
        ) from exc

    return ResponseEnvelope[FreshStartResult](data=result)


@router.post(
    "/schwab-json",
    response_model=ResponseEnvelope[ImportResult],
    status_code=status.HTTP_200_OK,
    summary="Import Schwab transactions from JSON body",
    description=(
        "Import Schwab transactions from a wrapped JSON object body "
        "({fetchedAt, dateRange, accounts:[...]}). The target account must be "
        "a SCHWAB account. Returns 422 for Schwab account mismatch/ambiguity."
    ),
    responses={
        200: {"description": "Import completed."},
        400: {"description": "Import payload is invalid or cannot be parsed."},
        401: {"description": "Authentication required."},
        404: {"description": "Account not found or not owned by the current user."},
        422: {"description": "Account type is not SCHWAB or Schwab account mismatch/ambiguity."},
    },
)
async def import_schwab_json(
    portfolio_id: uuid.UUID,
    service: Annotated[ImportService, Depends(_get_service)],
    session: Annotated[AsyncSession, Depends(get_session_with_rls)],
    current_user: Annotated[User, Depends(get_current_user)],
    account_id: Annotated[
        uuid.UUID,
        Query(description="UUID of the Schwab account to import into."),
    ],
    body: Annotated[dict[str, object], Body()],
) -> ResponseEnvelope[ImportResult]:
    """Import Schwab transaction JSON directly from request body."""
    account = await AccountRepository(session).get_by_id(
        account_id,
        current_user.id,
        portfolio_id,
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found for user {current_user.id}.",
        )
    if account.type != AccountType.SCHWAB:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Account type must be SCHWAB for this endpoint",
        )

    try:
        content = json.dumps(body).encode("utf-8")
        result = await service.import_trades(
            content=content,
            filename="schwab_transactions.json",
            broker=BrokerType.SCHWAB,
            portfolio_id=portfolio_id,
            account_id=account_id,
            user_id=current_user.id,
        )
    except (ImportParseError, UnsupportedBrokerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ImportAccountMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unhandled exception during import_trades")

        if isinstance(exc, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Validation error during import parsing: {exc}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process import file. An unexpected error occurred: {exc}",
        ) from exc

    return ResponseEnvelope[ImportResult](data=result)


@router.patch(
    "/broker-raw-transactions/{raw_id}/suppress",
    response_model=ResponseEnvelope[BrokerRawTransactionRead],
    status_code=status.HTTP_200_OK,
    summary="Suppress or un-suppress a broker raw transaction",
    description=(
        "Toggle the ``suppressed`` flag on a BrokerRawTransaction. "
        "When ``suppressed=true``, the raw transaction is permanently excluded "
        "from all future imports (including fresh-start). "
        "To un-suppress, set ``suppressed=false``."
    ),
    responses={
        200: {"description": "Suppressed flag updated."},
        401: {"description": "Authentication required."},
        404: {"description": "BrokerRawTransaction not found."},
    },
)
async def suppress_raw_transaction(
    portfolio_id: uuid.UUID,
    raw_id: str,
    service: Annotated[ImportService, Depends(_get_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    suppressed: Annotated[
        bool,
        Query(description=("New value for the suppressed flag. True to suppress, False to restore.")),
    ] = True,
) -> ResponseEnvelope[BrokerRawTransactionRead]:
    """Toggle the suppressed flag on a broker raw transaction.

    Args:
        raw_id: The composite ID of the raw transaction.
        service: Injected import service.
        current_user: The authenticated user.
        suppressed: When True, suppress the BRT. When False, un-suppress it.

    Returns:
        The updated BrokerRawTransaction.

    Raises:
        HTTPException: 404 if the raw transaction is not found.
    """
    try:
        await service.suppress_raw_transaction(
            raw_id=raw_id,
            user_id=current_user.id,
            portfolio_id=portfolio_id,
            suppressed=suppressed,
        )
    except BrokerRawTransactionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    # Reload and return updated BRT
    raw = await service._raw_repo.get_by_id(raw_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"BrokerRawTransaction {raw_id} not found.",
        )

    return ResponseEnvelope(data=BrokerRawTransactionRead.model_validate(raw))


@router.post(
    "/flex",
    response_model=ResponseEnvelope[ImportResult],
    status_code=status.HTTP_200_OK,
    summary="Import trades via IBKR Flex Query API",
    description=(
        "Automatically fetch a Flex Query report from the IBKR Flex Web "
        "Service and import it through the same unified import pipeline used "
        "by file uploads. The server must have ``IBKR_FLEX_TOKEN`` configured. "
        "Generic accounts are file-only and are rejected with HTTP 422; use the generic upload endpoint instead."
    ),
    response_description=("A summary of the import operation wrapped in a response envelope, including the number of trades created and any warnings."),
    responses={
        200: {"description": "Import completed. Check warnings for skipped entries."},
        400: {"description": "Flex API returned a non-transient error, the fetched XML could not be parsed, or an unexpected error occurred."},
        401: {"description": "Authentication required."},
        404: {"description": "Account not found or not owned by the current user."},
        422: {"description": "Request validation failed, a generic account was supplied, or the shared import pipeline reported an account mismatch."},
        429: {"description": "IBKR Flex API rate limit exceeded."},
        503: {"description": "IBKR_FLEX_TOKEN not configured on this server, or the Flex API is temporarily unavailable — retry later, not immediately."},
    },
)
async def flex_import(
    portfolio_id: uuid.UUID,
    flex_service: Annotated[FlexImportService, Depends(_get_flex_service)],
    import_service: Annotated[ImportService, Depends(_get_service)],
    account_service: Annotated[AccountService, Depends(_get_account_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    body: Annotated[FlexImportRequest, Body()],
    account_id: Annotated[
        uuid.UUID | None,
        Query(description="Optional UUID of the account to import trades into."),
    ] = None,
) -> ResponseEnvelope[ImportResult]:
    """Fetch an IBKR Flex Query report and import the trades it contains.

    Args:
        portfolio_id: UUID of the owned portfolio for this import.
        flex_service: Injected Flex API client service.
        import_service: Injected unified import service.
        account_service: Injected service used for optional account ownership lookup.
        current_user: The authenticated user.
        account_id: Optional account UUID supplied as a query parameter.
            When provided, it takes precedence over ``body.account_id``;
            the body field remains supported for callers that only send
            it in the JSON payload.
        body: Request payload with ``query_id``, optional ``account_id``,
            and optional ``from_date``.

    Returns:
        A :class:`~app.schemas.common.ResponseEnvelope` wrapping an
        :class:`~app.schemas.imports.ImportResult`.

    Raises:
        HTTPException: 400 if the Flex API returns a non-transient error,
            the fetched XML cannot be parsed, or another unexpected error
            occurs while importing.
        HTTPException: 404 if the account is not found or not owned by
            the current user.
        HTTPException: 422 if the shared import pipeline reports an
            account mismatch, a generic account is supplied, or request
            validation fails. Generic accounts support CSV/JSON uploads only.
        HTTPException: 429 if the IBKR rate limit is hit.
        HTTPException: 503 if the Flex token is not configured, or the
            Flex API reports a transient generation delay. IBKR's own
            guidance is to wait minutes, not seconds, before retrying —
            this endpoint does not retry automatically.
    """
    resolved_account_id = account_id if account_id is not None else body.account_id

    try:
        if resolved_account_id is not None:
            try:
                account = await account_service.get_account(
                    resolved_account_id,
                    current_user.id,
                    portfolio_id,
                )
            except AccountNotFoundError:
                raise AccountNotFoundError(f"Account {resolved_account_id} not found for user {current_user.id}.") from None
            if account.type == AccountType.GENERIC:
                raise ImportNotAllowedError(GENERIC_ACCOUNT_API_ERROR)

        xml_content = await flex_service.fetch_flex_report(
            query_id=body.query_id,
            from_date=body.from_date,
        )
        result = await import_service.import_trades(
            content=xml_content,
            filename=f"flex_query_{body.query_id}.xml",
            broker=BrokerType.IBKR,
            portfolio_id=portfolio_id,
            account_id=resolved_account_id,
            user_id=current_user.id,
            overwrite_manually_edited=body.overwrite_manually_edited,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(f"IBKR Flex API rate limit exceeded. Retry after {exc.wait_seconds} seconds."),
            headers={"Retry-After": str(exc.wait_seconds)},
        ) from exc
    except FlexAPIError as exc:
        if is_transient_send_request_error(str(exc)):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(f"IBKR Flex API temporarily unavailable: {exc}"),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (ImportParseError, UnsupportedBrokerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ImportAccountMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ImportNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unhandled exception during flex import")

        if isinstance(exc, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Validation error during import parsing: {exc}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process Flex import. An unexpected error occurred: {exc}",
        ) from exc

    return ResponseEnvelope[ImportResult](data=result)
