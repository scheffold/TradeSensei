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

"""Pydantic DTOs for the broker file import endpoint."""

import uuid

from pydantic import BaseModel, Field

from app.core.constants import GENERIC_IMPORT_MAX_ERRORS


class GenericImportRowError(BaseModel):
    """One safe generic validation error, or the final truncation marker."""

    location: int | str = Field(
        description=(
            "Physical CSV line number including the header, a zero-based JSON array location, "
            "or 'document' for document-level errors and the truncation marker."
        ),
        examples=[2, "executions[0]", "corporate_actions[1]", "document"],
    )
    field: str = Field(
        description="Invalid field name, or a document-level field such as header, encoding or record. The truncation marker uses 'errors'.",
        examples=["executed_at", "instrument_id", "errors"],
    )
    message: str = Field(
        description="Validation rule explanation naming the field without echoing supplied values, or a count of omitted errors.",
        examples=["executed_at requires an explicit timezone offset or Z.", "2 more validation errors omitted."],
    )


class GenericImportValidationDetail(BaseModel):
    """Structured HTTP 400 detail for file validation or persisted order-marker conflicts."""

    message: str = Field(description="Safe summary of the generic validation failure.", examples=["Generic import validation failed."])
    errors: list[GenericImportRowError] = Field(
        max_length=GENERIC_IMPORT_MAX_ERRORS + 1,
        description=(
            f"At most {GENERIC_IMPORT_MAX_ERRORS} row errors. If more errors exist, a final location='document', field='errors' "
            f"marker states how many were omitted, making at most {GENERIC_IMPORT_MAX_ERRORS + 1} entries including the marker."
        ),
        examples=[[{"location": 2, "field": "execution_id", "message": "execution_id is required."}]],
    )


class ImportBadRequestResponse(BaseModel):
    """Documentation-only schema for existing import HTTP 400 response bodies."""

    detail: str | GenericImportValidationDetail = Field(
        description=(
            "Legacy string detail for parse/processing errors or an unacknowledged fresh-start; "
            "generic validation and persisted order-marker conflicts instead return a message and bounded safe field errors. No response envelope is used."
        ),
        examples=[
            "Generic import supports CSV and JSON only; XML is not supported.",
            {
                "message": "Generic import validation failed.",
                "errors": [{"location": "executions[0]", "field": "execution_id", "message": "execution_id is required."}],
            },
        ],
    )


class FreshStartResult(BaseModel):
    """Result of a fresh-start import operation.

    Attributes:
        account_id: The account that was reset.
        executions_deleted: Number of LogicalExecution rows deleted.
        trades_deleted: Number of Trade rows deleted.
        import_result: The result of the import that followed the reset.
    """

    account_id: uuid.UUID = Field(
        ...,
        description="UUID of the account that was reset.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    executions_deleted: int = Field(
        ...,
        ge=0,
        description="Number of LogicalExecution rows deleted during reset.",
        examples=[4, 0],
    )
    trades_deleted: int = Field(
        ...,
        ge=0,
        description="Number of Trade rows deleted during reset.",
        examples=[2, 0],
    )
    import_result: "ImportResult" = Field(
        ...,
        description="Import result from the file processed after the reset.",
        examples=[{"broker": "generic", "filename": "executions.csv", "trades_parsed": 1, "trades_created": 1, "trades_skipped": 0}],
    )


class ClearTradesResult(BaseModel):
    """Result of clearing all trade data for an account.

    Attributes:
        trades_deleted: Number of Trade rows deleted.
        executions_deleted: Number of LogicalExecution rows deleted.
    """

    trades_deleted: int = Field(
        ...,
        ge=0,
        description="Number of Trade rows deleted during account reset.",
    )
    executions_deleted: int = Field(
        ...,
        ge=0,
        description="Number of LogicalExecution rows deleted during account reset.",
    )


class ImportResult(BaseModel):
    """Result of a broker trade import operation.

    Attributes:
        broker: Broker identifier used for this import (e.g. ``"ibkr"``).
        filename: Source label supplied by the caller.
        trades_parsed: Total number of trade rows successfully parsed from
            the file.
        trades_created: Number of trades actually persisted to the database.
        trades_skipped: Number of rows that were skipped due to parse
            errors or other non-fatal issues.
        raw_transactions_created: Number of broker raw transactions newly
            inserted during this import.
        raw_suppressed_skipped: Number of BrokerRawTransaction records
            skipped because their ``suppressed`` flag is ``True``.
        raw_deleted_skipped: Number of BrokerRawTransaction records skipped
            because their linked execution has ``deleted=True`` (user intent
            preserved — execution was previously deleted by the user).
        trades_protected_reconciled: Number of metadata-protected trades
            encountered during rebuild and skipped from structural updates.
        trades_protected_skipped: Number of metadata-protected positions
            skipped because no existing trade could be resolved for them.
        protected_positions_closed: Number of executions linked to open
            metadata-protected trades via seed-lot matching.
        unlinked_executions: Number of executions left unlinked because their
            target contract belongs to a protected trade.
        warnings: Human-readable descriptions of each skipped row.
        activity_log: Informational log of routine created/updated rows.
    """

    broker: str = Field(
        ...,
        description="Broker/parser identifier used for this import: 'ibkr', 'schwab' or 'generic'.",
        examples=["ibkr", "schwab", "generic"],
    )
    filename: str = Field(
        ...,
        description=(
            "Source label supplied by the caller. For uploaded files this is "
            "the original filename; for API-driven imports it may be a synthetic "
            "label such as `flex_query_{query_id}.xml` or "
            "`schwab_transactions_sync.json`."
        ),
        examples=[
            "activity_statement.csv",
            "flex_query_123456.xml",
            "schwab_transactions_sync.json",
        ],
    )
    trades_parsed: int = Field(
        ...,
        ge=0,
        description=("Total number of trade rows successfully parsed from the file. This is the sum of trades_created and trades_skipped."),
        examples=[42, 0],
    )
    trades_created: int = Field(
        ...,
        ge=0,
        description="Number of trades that were persisted to the database.",
        examples=[40, 0],
    )
    trades_skipped: int = Field(
        ...,
        ge=0,
        description=("Number of rows that were skipped due to non-fatal parse errors. Inspect the warnings list for details."),
        examples=[2, 0],
    )
    raw_transactions_created: int = Field(
        default=0,
        ge=0,
        description=("Number of broker raw transactions newly inserted during this import. Re-importing identical files should produce 0."),
        examples=[339, 0],
    )
    raw_suppressed_skipped: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of raw broker transactions skipped because their suppressed flag is true. "
            "Ordinary re-imports honor suppression; destructive history resets delete the raw records and their suppression flags."
        ),
        examples=[0, 3],
    )
    raw_deleted_skipped: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of raw broker transactions skipped because their linked "
            "execution was previously deleted by the user (``deleted=True``). "
            "Ordinary re-import does not recreate them; a destructive reset clears this history."
        ),
        examples=[0, 1],
    )
    trades_protected_reconciled: int = Field(
        default=0,
        ge=0,
        description=("Number of metadata-protected trades (``is_manually_edited=True``) encountered during rebuild and skipped from structural updates."),
        examples=[0, 1],
    )
    trades_protected_skipped: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of metadata-protected positions skipped during this import "
            "because no existing trade could be resolved to reconcile them "
            "onto. See ``warnings`` for the affected symbol/date."
        ),
        examples=[0, 1],
    )
    protected_positions_closed: int = Field(
        default=0,
        ge=0,
        description=("Number of executions linked to open metadata-protected trades via seed-lot matching during projection rebuild."),
        examples=[0, 1],
    )
    unlinked_executions: int = Field(
        default=0,
        ge=0,
        description=("Number of executions left unlinked because their target contract belongs to a protected trade isolated from rebuild."),
        examples=[0, 2],
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal import warnings, including skipped rows and ignored generic CSV columns. "
            "Invalid generic rows reject the whole file with HTTP 400 rather than appearing as skipped-row warnings."
        ),
        examples=[
            ["IBKR row 3: skipped — Row has only 5 columns; expected at least 16."],
            ["Unknown CSV column ignored: broker_note."],
            [],
        ],
    )
    activity_log: list[str] = Field(
        default_factory=list,
        description=(
            "Informational log of routine, successful row-level outcomes "
            "('created:'/'updated:' lines). Not an error or warning signal — "
            "kept separate from `warnings` so callers never mistake normal "
            "import activity for a problem."
        ),
        examples=[
            ["created: trade AAPL opened 2024-03-15T09:30:00Z"],
            [],
        ],
    )


class FlexImportRequest(BaseModel):
    """Request body for the IBKR Flex Query automatic import endpoint.

    Attributes:
        query_id: IBKR Flex Query ID configured in Account Management.
        account_id: Optional UUID of the broker account to associate
            imported trades with.
        from_date: Optional start date (``YYYYMMDD``) to restrict the
            report period sent to the Flex API.
    """

    query_id: str = Field(
        ...,
        min_length=1,
        description=("IBKR Flex Query ID as configured in the IBKR Account Management portal under Reports → Flex Queries."),
        examples=["123456"],
    )
    account_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID of the broker account to associate imported trades with; generic accounts are rejected because they support file imports only. "
            "The account_id query parameter takes precedence. Omit both to create trades without an account association."
        ),
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6", None],
    )
    from_date: str | None = Field(
        default=None,
        pattern=r"^\d{8}$",
        description=("Optional start date in ``YYYYMMDD`` format. When supplied, only trades on or after this date are included in the Flex Query report."),
        examples=["20240101", None],
    )
    overwrite_manually_edited: bool = Field(
        default=False,
        description=("When true, trades that have been manually edited are no longer protected and will be overwritten by the incoming import data."),
        examples=[False, True],
    )
