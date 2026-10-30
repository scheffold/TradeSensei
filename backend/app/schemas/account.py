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

"""Pydantic DTOs for Account request and response shapes."""

import uuid
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.constants import (
    ACCOUNT_NAME_MAX_LENGTH,
    ACCOUNT_NAME_MIN_LENGTH,
    ACCOUNT_NUMBER_MAX_LENGTH,
    GENERIC_ACCOUNT_CREDENTIALS_ERROR,
)
from app.models.account import AccountType

# ── Import/sync settings per account ─────────────────────────────────────────

FlexFromDatePreset = Literal["7d", "30d", "90d", "180d", "360d", "mtd", "ytd", "custom"]


class AccountSettings(BaseModel):
    """Per-account import and sync preferences.

    These settings are stored as a JSONB blob on the ``accounts`` row and
    are loaded alongside the account on every read.

    Attributes:
        sync_days: Number of past days to fetch when using the broker API
            sync endpoint.  Defaults to ``30``.
        flex_from_date_preset: Controls the Flex import date-range mode.
            Supports dynamic offsets ('7d', '30d', '90d', '180d', '360d'),
            'mtd' (month-to-date), 'ytd' (year-to-date), or 'custom' for
            an explicit ``flex_custom_from_date`` value.
        flex_custom_from_date: Explicit start date (``YYYYMMDD``) used when
            ``flex_from_date_preset`` is ``"custom"``.  ``None`` otherwise.
    """

    sync_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description=("Number of past days to pull via broker API sync. Must be between 1–365."),
        examples=[7, 30, 90, 180, 365],
    )
    flex_from_date_preset: FlexFromDatePreset = Field(
        default="ytd",
        description=("Controls the Flex import date-range mode: 'ytd' = January 1st of the current year, 'custom' = the explicit flex_custom_from_date value."),
        examples=["ytd", "custom"],
    )
    flex_custom_from_date: str | None = Field(
        default=None,
        description=("Explicit Flex import start date in YYYYMMDD format. Only used when flex_from_date_preset is 'custom'."),
        examples=["20250101", None],
    )

    @field_validator("flex_custom_from_date")
    @classmethod
    def validate_date_format(cls, v: str | None) -> str | None:
        """Validate that the date string matches YYYYMMDD format."""
        if v is None:
            return v
        if len(v) != 8 or not v.isdigit():
            raise ValueError("flex_custom_from_date must be in YYYYMMDD format.")
        return v


class AccountCreate(BaseModel):
    """Request schema for creating a new trading account.

    Attributes:
        name: Human-readable account name (e.g. "Interactive Brokers Main").
        account_number: Optional broker account number (e.g. "U1234567").
        type: Account/broker type. Defaults to ``MANUAL``.
        properties: Optional mapping of broker credential keys to plaintext
            values.  The service layer encrypts each value before storage.
            Pass ``null`` or omit to create an account without credentials.
            Generic accounts reject non-empty maps and support file imports only.
        settings: Optional per-account import/sync settings. Defaults apply
            when omitted.
        portfolio_id: Optional legacy body field; the API uses the owned path
            portfolio_id and inherits the account currency from that portfolio.
    """

    name: str = Field(
        ...,
        min_length=ACCOUNT_NAME_MIN_LENGTH,
        max_length=ACCOUNT_NAME_MAX_LENGTH,
        description="Human-readable account label (e.g. 'Interactive Brokers Main').",
        examples=["Interactive Brokers Main", "Schwab Cash Account"],
    )
    account_number: str | None = Field(
        default=None,
        max_length=ACCOUNT_NUMBER_MAX_LENGTH,
        description="Optional broker account number (e.g. 'U1234567').",
        examples=["U1234567", None],
    )
    type: AccountType = Field(
        default=AccountType.MANUAL,
        description=(
            "Account/broker type: 'schwab' for Charles Schwab, 'ibkr' for Interactive Brokers, "
            "'generic' for CSV/JSON-only imports from other brokers, or 'manual'. Generic accounts support no credentials or API sync."
        ),
        examples=["manual", "schwab", "ibkr", "generic"],
    )
    properties: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional map of broker credential keys to plaintext values "
            "(e.g. {'SCHWAB_APP_KEY': '...', 'SCHWAB_APP_SECRET': '...'}).  "
            "Values are encrypted at rest. Pass null or omit to skip. For generic accounts, omit, pass null or use an empty map; non-empty maps are rejected."
        ),
        examples=[
            {"SCHWAB_APP_KEY": "abc123", "SCHWAB_APP_SECRET": "xyz789"},
            None,
        ],
    )
    settings: AccountSettings | None = Field(
        default=None,
        description="Optional per-account import/sync settings. Generic accounts do not support API sync regardless of these settings.",
        examples=[{"sync_days": 30, "flex_from_date_preset": "ytd"}, None],
    )
    portfolio_id: uuid.UUID | None = Field(
        default=None,
        description="Optional legacy body field. The account creation API always uses the owned portfolio_id from the path instead.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6", None],
    )

    @model_validator(mode="after")
    def reject_generic_credentials(self) -> Self:
        """Reject credential properties on file-only generic accounts."""
        if self.type == AccountType.GENERIC and self.properties:
            raise ValueError(GENERIC_ACCOUNT_CREDENTIALS_ERROR)
        return self


class AccountUpdate(BaseModel):
    """Request schema for partially updating a trading account.

    All fields are optional — only supplied fields are written.

    Attributes:
        name: Updated account name.
        account_number: Updated broker account number. Pass ``null`` to clear.
        type: Updated account/broker type. Changes to/from generic require no
            raw, logical or trade history, including suppressed/deleted rows;
            transitions through manual do not bypass this rule. The history
            check and update are serialized with account-targeted ingestion.
        is_active: Updated active flag.
        properties: Optional map of broker credential keys to updated
            plaintext values.  Existing properties not included here are
            left unchanged (merge semantics). Generic accounts reject non-empty
            maps, including when type is omitted. Accounts with stored
            credentials cannot be switched to generic.
        settings: Optional updated import/sync settings. Merges with existing
            settings (partial update semantics).
    """

    name: str | None = Field(
        default=None,
        min_length=ACCOUNT_NAME_MIN_LENGTH,
        max_length=ACCOUNT_NAME_MAX_LENGTH,
        description="Updated account label. Omit to leave unchanged.",
        examples=["IBKR Futures Account", None],
    )
    account_number: str | None = Field(
        default=None,
        max_length=ACCOUNT_NUMBER_MAX_LENGTH,
        description="Updated broker account number. Pass null to clear.",
        examples=["U7654321", None],
    )
    type: AccountType | None = Field(
        default=None,
        description=(
            "Updated account/broker type. Omit to leave unchanged. 'generic' supports CSV/JSON file imports only, with no credentials or API sync. "
            "Switching an account with stored credential properties to 'generic' is rejected. "
            "Changes to/from 'generic' require no raw transactions, logical executions or trades, including suppressed/deleted history. "
            "Transitions through 'manual' do not bypass the guard. The check/update is serialized with ingestion for the account. "
            "Use a new account or explicitly clear all history first; credential restrictions still apply."
        ),
        examples=["schwab", "generic", None],
    )
    is_active: bool | None = Field(
        default=None,
        description="Updated active flag. Omit to leave unchanged.",
        examples=[False, None],
    )
    properties: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional credential map. Upserts supplied keys; unlisted keys remain unchanged. "
            "Non-empty maps are rejected if the effective type is generic (the existing type when omitted, otherwise the requested type)."
        ),
        examples=[
            {"SCHWAB_APP_KEY": "new-key-value"},
            None,
        ],
    )
    settings: AccountSettings | None = Field(
        default=None,
        description="Optional updated import/sync settings. Omit to leave unchanged. These settings do not enable API sync for generic accounts.",
        examples=[{"sync_days": 90}, None],
    )

    @model_validator(mode="after")
    def reject_generic_credentials(self) -> Self:
        """Reject explicit generic type/credential combinations at the boundary."""
        if self.type == AccountType.GENERIC and self.properties:
            raise ValueError(GENERIC_ACCOUNT_CREDENTIALS_ERROR)
        return self


class AccountRead(BaseModel):
    """Response schema for a single trading account.

    Attributes:
        id: UUID primary key.
        user_id: UUID of the owning user.
        name: Account label.
        account_number: Optional broker account number.
        type: Account/broker type.
        currency: ISO-4217 currency code.
        is_active: Whether the account is active.
        settings: Per-account import/sync preferences. Returns defaults if
            no settings have been saved yet.
        created_at: UTC timestamp of account creation.
        has_api_keys: ``True`` if the account has any stored broker
            credentials (computed by the service layer, never read directly
            from the ORM).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(
        ...,
        description="Unique account identifier (UUID v4).",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    user_id: uuid.UUID = Field(
        ...,
        description="UUID of the user who owns this account.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    portfolio_id: uuid.UUID = Field(
        ...,
        description="UUID of the portfolio this account belongs to.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    portfolio_name: str | None = Field(
        default=None,
        description="Name of the portfolio this account belongs to.",
        examples=["Default Portfolio", None],
    )
    name: str = Field(
        ...,
        description="Human-readable account label.",
        examples=["Interactive Brokers Main"],
    )
    account_number: str | None = Field(
        default=None,
        description="Optional broker account number (e.g. 'U1234567').",
        examples=["U1234567", None],
    )
    type: AccountType = Field(
        ...,
        description="Account/broker type. 'generic' means generic v1 CSV/JSON file imports only, without broker credentials or API sync.",
        examples=["manual", "schwab", "ibkr", "generic"],
    )
    currency: str = Field(
        ...,
        description="ISO-4217 account currency inherited from the portfolio's base_currency at creation.",
        examples=["USD"],
    )
    is_active: bool = Field(
        ...,
        description="Whether this account is currently active.",
        examples=[True],
    )
    settings: AccountSettings = Field(
        default_factory=AccountSettings,
        description=("Per-account import/sync settings. Returns defaults if not yet configured."),
        examples=[{"sync_days": 30, "flex_from_date_preset": "ytd", "flex_custom_from_date": None}],
        validate_default=True,
    )

    @field_validator("settings", mode="before")
    @classmethod
    def validate_settings(cls, v: Any) -> Any:
        """Ensure settings is never None; return default AccountSettings instead."""
        if v is None:
            return {}
        return v

    created_at: AwareDatetime = Field(
        ...,
        description="UTC timestamp of account creation.",
        examples=["2024-03-15T09:30:00Z"],
    )
    has_api_keys: bool = Field(
        default=False,
        description="True if the account has at least one stored broker credential. Computed by the service layer; false for generic accounts.",
        examples=[True, False],
    )


class AccountCredentialsRead(BaseModel):
    """Response schema for plaintext account credentials.

    Attributes:
        properties: Map of credential keys to plaintext values.
    """

    properties: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Plaintext account credential values keyed by property name. Returned only to the owning authenticated user. "
            "Generic accounts reject credential reads with HTTP 422 rather than returning this DTO."
        ),
        examples=[
            {"ibkr_flex_token": "abc123", "ibkr_flex_query_id": "98765"},
            {"schwab_app_key": "key-123", "schwab_app_secret": "secret-456"},
        ],
    )


class AccountMetrics(BaseModel):
    """Aggregate metrics for a single trading account.

    Attributes:
        total_dividends: Total dividends received in this account (in EUR).
        total_withholding_tax: Total withholding tax paid in this account (in EUR).
    """

    total_dividends: Decimal = Field(
        default=Decimal("0.0"),
        description="Total dividends received across this account.",
        examples=["150.50"],
    )
    total_withholding_tax: Decimal = Field(
        default=Decimal("0.0"),
        description="Total withholding tax paid across this account.",
        examples=["-22.50"],
    )
