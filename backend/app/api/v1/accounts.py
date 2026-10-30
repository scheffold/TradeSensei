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

"""Route handlers for the ``/api/v1/accounts`` resource.

Provides full CRUD on trading accounts, scoped to the authenticated user.
All endpoints require a valid Bearer token.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_portfolio_owner
from app.core.deps import get_current_user, get_session_with_rls
from app.core.exceptions import AccountNotFoundError, AccountValidationError, ConfigurationError
from app.models.user import User
from app.schemas.account import (
    AccountCreate,
    AccountCredentialsRead,
    AccountMetrics,
    AccountRead,
    AccountUpdate,
)
from app.schemas.broker import BrokerConnectionTestRequest, BrokerConnectionTestResult
from app.schemas.common import CursorPage, ResponseEnvelope
from app.schemas.imports import ClearTradesResult
from app.services.account_service import AccountService
from app.services.broker_service import BrokerService

router = APIRouter(
    prefix="/portfolios/{portfolio_id}/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_portfolio_owner)],
)


def _get_service(
    session: Annotated[AsyncSession, Depends(get_session_with_rls)],
) -> AccountService:
    """FastAPI dependency that constructs an :class:`AccountService`.

    Args:
        session: Injected async database session.

    Returns:
        An :class:`AccountService` bound to the current session.
    """
    return AccountService(session)


def _get_broker_service(
    session: Annotated[AsyncSession, Depends(get_session_with_rls)],
) -> BrokerService:
    return BrokerService(session)


@router.get(
    "",
    response_model=ResponseEnvelope[CursorPage[AccountRead]],
    summary="List Trading Accounts",
    description=(
        "Return a cursor-paginated list of all trading accounts owned by the "
        "currently authenticated user, ordered by creation date descending. "
        "Accounts belonging to other users are never included in the response."
    ),
    response_description=("A paginated list of accounts wrapped in a response envelope, with a ``next_cursor`` token when further pages exist."),
    responses={
        200: {"description": "Account list returned successfully."},
        401: {"description": "Missing or invalid Bearer token."},
    },
)
async def list_accounts(
    portfolio_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[CursorPage[AccountRead]]:
    """Return all accounts for the authenticated user.

    Fetches every trading account scoped to the calling user's ID and
    wraps the results in a :class:`CursorPage`. Currently returns all
    accounts in a single page; cursor-based filtering will be wired in a
    future iteration.

    Args:
        current_user: Injected authenticated user ORM instance.
        service: Injected account service.

    Returns:
        A :class:`ResponseEnvelope` wrapping a :class:`CursorPage` of
        :class:`AccountRead` DTOs.
    """
    page = await service.list_accounts(current_user.id, portfolio_id)
    return ResponseEnvelope[CursorPage[AccountRead]](data=page)


@router.post(
    "/test-connection",
    response_model=ResponseEnvelope[BrokerConnectionTestResult],
    status_code=status.HTTP_200_OK,
    summary="Test a broker API connection",
    description=(
        "Validate supplied (or stored) broker credentials for IBKR or Schwab without creating or updating an account. "
        "Only 'ibkr' and 'schwab' are valid payload types. Referencing an existing generic account returns HTTP 400 "
        "because generic accounts support CSV/JSON file imports only, without credentials or API connections."
    ),
    response_description="Credential validation result wrapped in a response envelope.",
    responses={
        200: {"description": "Connection test succeeded."},
        400: {"description": "Credential/configuration validation failed, or account_id references a generic account with no API support."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Portfolio not owned/found, or supplied account_id not found in the owned path portfolio."},
        422: {"description": "Request validation failed; payload type must be 'ibkr' or 'schwab', never 'generic' or 'manual'."},
    },
)
async def test_connection(
    portfolio_id: uuid.UUID,
    payload: BrokerConnectionTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    broker_service: Annotated[BrokerService, Depends(_get_broker_service)],
) -> ResponseEnvelope[BrokerConnectionTestResult]:
    """Validate broker credentials for the current user and portfolio.

    Args:
        portfolio_id: UUID of the owned portfolio used for account lookup.
        payload: IBKR/Schwab credential overrides and an optional existing account.
        current_user: The authenticated account owner.
        broker_service: Injected broker credential validation service.

    Returns:
        A response envelope containing the connection test result.

    Raises:
        HTTPException: 400 if credential/configuration validation fails or an
            existing generic account is supplied.
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the portfolio or supplied account is not owned/found.
        HTTPException: 422 if the request is invalid, including payload type
            'generic'; only 'ibkr' and 'schwab' support connection tests.
    """
    try:
        result = await broker_service.test_connection(
            payload=payload,
            user_id=current_user.id,
            portfolio_id=portfolio_id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ResponseEnvelope[BrokerConnectionTestResult](data=result)


@router.post(
    "",
    response_model=ResponseEnvelope[AccountRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a trading account",
    description=(
        "Create a new trading account owned by the currently authenticated user. "
        "The path portfolio_id determines the owned portfolio; account currency is inherited from its base_currency. "
        "Supported types are 'manual', 'ibkr', 'schwab' and 'generic'. Generic accounts accept generic v1 CSV/JSON file imports only, "
        "do not store credentials and cannot use API sync. A non-empty properties map for a generic account returns HTTP 422."
    ),
    response_description="The newly created account wrapped in a response envelope.",
    responses={
        201: {"description": "Account created successfully."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Path portfolio not found or not owned by the current user."},
        422: {"description": "Request validation failed, including non-empty credential properties for a generic account."},
    },
)
async def create_account(
    portfolio_id: uuid.UUID,
    payload: AccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[AccountRead]:
    """Create a new trading account for the authenticated user.

    The account is automatically scoped to the calling user's ID; callers
    cannot create accounts on behalf of other users. The path portfolio
    supplies both portfolio ownership and account currency. Generic accounts
    are file-only and reject non-empty credential properties.

    Args:
        portfolio_id: UUID of the owned portfolio to contain the account.
        payload: Account name, type and optional number, properties and settings.
        current_user: Injected authenticated user ORM instance.
        service: Injected account service.

    Returns:
        A :class:`ResponseEnvelope` containing the created
        :class:`AccountRead` DTO.

    Raises:
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the path portfolio is not found or not owned.
        HTTPException: 422 if request validation fails or a generic account
            includes non-empty credential properties.
    """
    account_payload = payload.model_copy(update={"portfolio_id": portfolio_id})
    try:
        account = await service.create_account(account_payload, current_user.id)
    except AccountValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ResponseEnvelope[AccountRead](data=account)


@router.get(
    "/{account_id}/credentials",
    response_model=ResponseEnvelope[AccountCredentialsRead],
    status_code=status.HTTP_200_OK,
    summary="Get account credentials",
    description=(
        "Return decrypted broker credentials for an account in the authenticated user's path portfolio. "
        "Generic accounts never store broker credentials and return HTTP 422 rather than an empty credential response."
    ),
    response_description="Plaintext credentials wrapped in a response envelope.",
    responses={
        200: {"description": "Credentials returned successfully."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Portfolio/account not found, not owned, or account outside the path portfolio."},
        422: {"description": "Generic accounts do not support broker credentials, or request parameters are invalid."},
    },
)
async def get_account_credentials(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[AccountCredentialsRead]:
    """Return decrypted account credentials for the authenticated owner.

    Args:
        portfolio_id: UUID of the owned portfolio containing the account.
        account_id: UUID of the account whose credentials are requested.
        current_user: The authenticated account owner.
        service: Injected account service.

    Returns:
        A response envelope containing the plaintext credential map.

    Raises:
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the portfolio/account is not owned/found or the
            account is outside the path portfolio.
        HTTPException: 422 if the account is generic or parameters are invalid.
    """
    try:
        credentials = await service.get_account_credentials(
            account_id=account_id,
            user_id=current_user.id,
            portfolio_id=portfolio_id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc
    except AccountValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ResponseEnvelope[AccountCredentialsRead](data=credentials)


@router.get(
    "/{account_id}",
    response_model=ResponseEnvelope[AccountRead],
    summary="Get a Trading Account",
    description=(
        "Retrieve the full details of a specific trading account by its UUID. "
        "Enforces ownership — returns 404 if the account does not exist **or** "
        "belongs to a different user, so as not to leak account IDs."
    ),
    response_description="The requested account wrapped in a response envelope.",
    responses={
        200: {"description": "Account found and returned."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Account not found (or owned by another user)."},
        422: {"description": "The supplied ``account_id`` is not a valid UUID."},
    },
)
async def get_account(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[AccountRead]:
    """Retrieve a single account owned by the authenticated user.

    The service layer enforces user scoping — an ``AccountNotFoundError`` is
    raised (and translated to HTTP 404) if the account does not exist or
    belongs to a different user.

    Args:
        account_id: UUID path parameter identifying the account.
        current_user: Injected authenticated user ORM instance.
        service: Injected account service.

    Returns:
        A :class:`ResponseEnvelope` containing the :class:`AccountRead` DTO.

    Raises:
        HTTPException: 404 if the account is not found for this user.
    """
    try:
        account = await service.get_account(account_id, current_user.id, portfolio_id)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc
    return ResponseEnvelope[AccountRead](data=account)


@router.patch(
    "/{account_id}",
    response_model=ResponseEnvelope[AccountRead],
    status_code=status.HTTP_200_OK,
    summary="Update a trading account",
    description=(
        "Apply a partial update to a trading account owned by the authenticated "
        "user. Only fields present in the request body are modified; omitted "
        "fields retain their current values. "
        "Returns 404 if the account is not in the owned path portfolio. Generic accounts support file imports only and reject "
        "non-empty credential properties, including when type is omitted. Switching an account with stored credentials to 'generic' is also rejected. "
        "Changing type to or from 'generic' requires no raw transactions, logical executions or trades; suppressed/deleted history still counts. "
        "Transitions involving 'manual' do not bypass this guard. The history check and type update share the account lock with ingestion. "
        "Use a new account or explicitly clear all history before switching, subject to the credential restrictions."
    ),
    response_description=("The account after applying the partial update, wrapped in a response envelope."),
    responses={
        200: {"description": "Account updated successfully."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Portfolio/account not found, not owned, or account outside the path portfolio."},
        422: {
            "description": (
                "Request validation or generic credential rules failed, or a type change to/from generic retains any raw/logical/trade history, "
                "including suppressed/deleted rows and transitions involving manual."
            )
        },
    },
)
async def update_account(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: AccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[AccountRead]:
    """Partially update a trading account owned by the authenticated user.

    Only the fields present in the request body are written. Fields that
    are absent (or explicitly ``None``) are not changed. Ownership is
    enforced — accounts belonging to other users raise HTTP 404. Non-empty
    credential properties are rejected when the effective type is generic;
    accounts with stored credentials cannot be switched to generic.
    Any type change to/from generic also requires no raw, logical or trade
    history, including suppressed/deleted rows. Passing through manual does
    not bypass the guard. History checks and updates are serialized against
    account-targeted ingestion. Use a new account or explicitly clear all
    history first; credential restrictions still apply.

    Args:
        portfolio_id: UUID of the owned portfolio containing the account.
        account_id: UUID path parameter identifying the account.
        payload: Partial name, number, type, active flag, properties or settings.
        current_user: Injected authenticated user ORM instance.
        service: Injected account service.

    Returns:
        A :class:`ResponseEnvelope` containing the updated
        :class:`AccountRead` DTO.

    Raises:
        HTTPException: 401 if authentication fails.
        HTTPException: 404 if the portfolio/account is not owned/found or the
            account is outside the path portfolio.
        HTTPException: 422 if validation fails or generic credential rules
            would be violated, or a type change to/from generic is attempted
            while any raw/logical/trade history remains.
    """
    try:
        account = await service.update_account(
            account_id,
            payload,
            current_user.id,
            portfolio_id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc
    except AccountValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ResponseEnvelope[AccountRead](data=account)


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a Trading Account",
    description=(
        "Permanently delete a trading account and all its associated trades. "
        "**This operation is irreversible.** "
        "Returns 404 if the account does not exist or belongs to a different "
        "user, so as not to leak account IDs."
    ),
    response_description="No content — the account and its trades have been deleted.",
    responses={
        204: {"description": "Account and associated trades deleted."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Account not found (or owned by another user)."},
        422: {"description": "The supplied ``account_id`` is not a valid UUID."},
    },
)
async def delete_account(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> None:
    """Delete a trading account owned by the authenticated user.

    Cascades the deletion to all trades associated with the account.
    Ownership is enforced — accounts belonging to other users raise
    HTTP 404 rather than HTTP 403, to avoid leaking account IDs.

    Args:
        account_id: UUID path parameter identifying the account.
        current_user: Injected authenticated user ORM instance.
        service: Injected account service.

    Raises:
        HTTPException: 404 if the account is not found for this user.
    """
    try:
        await service.delete_account(account_id, current_user.id, portfolio_id)
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        ) from exc


@router.delete(
    "/{account_id}/trades",
    response_model=ResponseEnvelope[ClearTradesResult],
    summary="Clear all trades for an account",
    description=(
        "Destructive operation — permanently deletes all trades, executions, "
        "and broker raw transactions for the specified account, while keeping "
        "the account itself. Set `acknowledged=true` to confirm."
    ),
    response_description="Deletion counts wrapped in a response envelope.",
    responses={
        200: {"description": "Account trade data cleared successfully."},
        400: {"description": "Acknowledgement flag is missing or false."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Account not found (or owned by another user)."},
    },
)
async def clear_account_trades(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
    acknowledged: Annotated[
        bool,
        Query(description="Must be true to confirm permanent deletion of all account trade data."),
    ] = False,
) -> ResponseEnvelope[ClearTradesResult]:
    """Delete all account trade data while preserving the account row."""
    if not acknowledged:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please set acknowledged=true to confirm this destructive action.",
        )

    try:
        result = await service.clear_account_trades(
            account_id=account_id,
            user_id=current_user.id,
            portfolio_id=portfolio_id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc

    return ResponseEnvelope[ClearTradesResult](data=result)


clear_account_trades.audit_metadata = {"skip_audit": True}  # type: ignore[attr-defined]


@router.get(
    "/{account_id}/metrics",
    response_model=ResponseEnvelope[AccountMetrics],
    summary="Get Account Cash Metrics",
    description=("Calculate and return aggregate cash metrics (e.g. total dividends and withholding tax) for the specified account."),
    response_description="Aggregate metrics wrapped in a response envelope.",
    responses={
        200: {"description": "Metrics calculated successfully."},
        401: {"description": "Missing or invalid Bearer token."},
        404: {"description": "Account not found (or owned by another user)."},
    },
)
async def get_account_metrics(
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AccountService, Depends(_get_service)],
) -> ResponseEnvelope[AccountMetrics]:
    """Retrieve aggregate cash metrics for the account.

    Args:
        account_id: UUID of the account.
        current_user: Authenticated user.
        service: Account service.

    Returns:
        ResponseEnvelope containing an AccountMetrics DTO.
    """
    try:
        metrics = await service.get_metrics(account_id, current_user.id, portfolio_id)
        return ResponseEnvelope[AccountMetrics](data=metrics)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.") from exc
