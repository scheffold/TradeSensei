"""Versioned broker-neutral execution and corporate-action DTOs."""

import re
from datetime import UTC, datetime
from datetime import date as date_
from decimal import Decimal
from typing import Literal, Self, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.config import JsonDict
from pydantic_core import PydanticCustomError

from app.core.constants import (
    CURRENCY_LENGTH,
    GENERIC_EXECUTION_ID_MAX_LENGTH,
    GENERIC_IMPORT_BROKER,
    GENERIC_IMPORT_DATE_PATTERN,
    GENERIC_IMPORT_DECIMAL_PATTERN,
    GENERIC_IMPORT_SCHEMA_VERSION_PATTERN,
    GENERIC_IMPORT_SOURCE_RECORD_KEY,
    GENERIC_INSTRUMENT_ID_MAX_LENGTH,
    GENERIC_PIPELINE_NET_CASH_KEY,
    GENERIC_PIPELINE_ORDER_ID_KEY,
    NUMERIC_PRECISION,
    NUMERIC_SCALE,
    SYMBOL_MAX_LENGTH,
    SYMBOL_MIN_LENGTH,
)
from app.models.trade import AssetClass, Direction
from app.schemas.broker_raw_transaction import BrokerRawTransactionCreate
from app.schemas.corporate_action import CorporateActionCreateRequest
from app.utils.occ import parse_occ_option

_RAW_DATA_ADAPTER = TypeAdapter(dict[str, object])
_REQUIRED_FIELD = object()
# Match the whitespace removed by the record normalizer's Python str.strip().
_INPUT_WHITESPACE = r"[\x09-\x0d\x1c-\x20\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]"
_INTEGER_WHITESPACE = _INPUT_WHITESPACE.replace(r"\x1c-\x20", r"\x20")


def _enum_input_schema(*values: str) -> JsonDict:
    alternatives = ("".join(f"[{letter}{letter.lower()}]" for letter in value) for value in values)
    return {"type": "string", "pattern": "^" + _INPUT_WHITESPACE + "*(?:" + "|".join(alternatives) + ")" + _INPUT_WHITESPACE + "*$"}


def _required_input_fields(*names: str) -> JsonDict:
    return {
        "required": list(names),
        "properties": {name: {"not": {"anyOf": [{"type": "null"}, {"type": "string", "pattern": "^" + _INPUT_WHITESPACE + "*$"}]}} for name in names},
    }


def _decimal_input_schema(schema: JsonDict, *names: str) -> None:
    """Constrain decimal text syntax and zero bounds, not normalized precision."""
    properties = cast(dict[str, JsonDict], schema["properties"])
    # Match Python str.strip(), including its extra Unicode whitespace, without
    # JavaScript's additional BOM whitespace accepting otherwise-invalid text.
    whitespace = _INPUT_WHITESPACE
    for name in names:
        field = properties[name]
        variants = cast(list[JsonDict], field.get("anyOf", []))
        number = next((variant for variant in variants if variant.get("type") == "number"), None)
        if number is None:  # Serialization schemas have no numeric input branch.
            continue
        guard = ""
        if number.get("exclusiveMinimum") == 0:
            guard = r"(?=\+?[0-9.]*[1-9])"
        elif number.get("minimum") == 0:
            guard = r"(?!-[0-9.]*[1-9])"
        pattern = "^" + whitespace + "*" + guard + "(?:" + GENERIC_IMPORT_DECIMAL_PATTERN + ")" + whitespace + "*$"
        for variant in variants:
            if variant.get("type") == "string":
                variant["pattern"] = pattern
        if "default" in field:
            variants.append({"type": "string", "pattern": "^" + whitespace + "*$"})
            if not any(variant.get("type") == "null" for variant in variants):
                variants.append({"type": "null"})
        field["$comment"] = (
            "String patterns enforce complete finite decimal syntax and sign/zero bounds. "
            "Normalized Numeric(18,8) precision, scale and exponent representability for strings and JSON numbers remain importer checks."
        )


def _corporate_action_input_schema(schema: JsonDict) -> None:
    _decimal_input_schema(schema, "ratio")


def _execution_input_schema(schema: JsonDict) -> None:
    """Describe pre-normalization inputs without changing runtime validation."""
    _decimal_input_schema(schema, "quantity", "price", "commission", "strike", "realized_pnl")
    properties = cast(dict[str, JsonDict], schema["properties"])
    for field, values in (
        ("asset_class", tuple(AssetClass)),
        ("side", ("BUY", "SELL")),
        ("open_close", ("OPEN", "CLOSE", "")),
        ("option_type", ("C", "P", "")),
    ):
        for key in ("$ref", "enum", "type", "anyOf"):
            properties[field].pop(key, None)
        enum_schema = _enum_input_schema(*values)
        properties[field].update({"anyOf": [enum_schema, {"type": "null"}]} if field in ("open_close", "option_type") else enum_schema)

    properties["multiplier"].pop("type", None)
    properties["multiplier"].pop("minimum", None)
    properties["multiplier"]["anyOf"] = [
        {"type": "integer", "minimum": 1},
        {"type": "string", "pattern": "^" + _INTEGER_WHITESPACE + r"*\+?(?=[0-9_]*[1-9])[0-9]+(?:_?[0-9]+)*(?:\.0+)?" + _INTEGER_WHITESPACE + "*$"},
        {"type": "string", "pattern": "^" + _INPUT_WHITESPACE + "*$"},
        {"type": "null"},
    ]
    order_present: JsonDict = {
        "required": ["order_id"],
        "properties": {"order_id": {"type": "string", "pattern": "[^" + _INPUT_WHITESPACE[1:-1] + "]"}},
    }
    # YY leap years follow parse_occ_option's 1951-2050 window (00 is 2000).
    occ_date = (
        r"(?:[0-9]{2}(?:(?:0[13578]|1[02])(?:0[1-9]|[12][0-9]|3[01])|(?:0[469]|11)(?:0[1-9]|[12][0-9]|30)|02(?:0[1-9]|1[0-9]|2[0-8]))"
        r"|(?:[02468][048]|[13579][26])0229)"
    )
    occ_symbol: JsonDict = {
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "pattern": r"^\s*\S[\s\S]*" + occ_date + r"[CcPp](?!00000000)[0-9]{8}\s*$"}},
    }
    schema["allOf"] = [
        {
            "if": {"required": ["asset_class"], "properties": {"asset_class": _enum_input_schema("OPTION", "FUTURE")}},
            "then": _required_input_fields("multiplier"),
        },
        {
            "if": {"required": ["asset_class"], "properties": {"asset_class": _enum_input_schema("FUTURE")}},
            "then": _required_input_fields("instrument_id"),
        },
        {
            "if": {"required": ["asset_class"], "properties": {"asset_class": _enum_input_schema("OPTION")}},
            "then": {"if": {"not": occ_symbol}, "then": _required_input_fields("instrument_id", "expiry", "strike", "option_type")},
        },
        {"if": order_present, "then": _required_input_fields("instrument_id", "open_close")},
        {
            "if": {"allOf": [order_present, {"required": ["open_close"], "properties": {"open_close": _enum_input_schema("CLOSE")}}]},
            "then": _required_input_fields("realized_pnl"),
        },
        {
            "if": {"required": ["open_close"], "properties": {"open_close": _enum_input_schema("OPEN")}},
            "then": {"not": _required_input_fields("realized_pnl")},
        },
    ]
    schema["$comment"] = (
        "Agreement between OCC symbol components and explicitly supplied contract fields is checked by the importer. "
        "Cross-record execution_id uniqueness, consistent order markers and distinct combo instrument_id values "
        "are checked by the parser, not by this row schema."
    )


def _validate_decimal_input(value: object) -> object:
    if isinstance(value, (bool, float)) or (isinstance(value, str) and re.fullmatch(GENERIC_IMPORT_DECIMAL_PATTERN, value.strip()) is None):
        raise PydanticCustomError("decimal_parsing", "Use a decimal without thousands separators.")
    return value


def _validate_date_input(value: object) -> object:
    if value is not None and type(value) is not date_ and (not isinstance(value, str) or re.fullmatch(GENERIC_IMPORT_DATE_PATTERN, value.strip()) is None):
        raise PydanticCustomError("date_parsing", "Use a date in YYYY-MM-DD format.")
    return value


class _GenericRecordV1(BaseModel):
    """Treat empty cells as absent while retaining the untouched source record."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, allow_inf_nan=False)

    _original_record: dict[str, object] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def _preserve_record(cls, value: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        if not isinstance(value, dict):
            return handler(value)
        original = cast(dict[str, object], value)
        normalized = {key: item for key, item in original.items() if item is not None and not (isinstance(item, str) and not item.strip())}
        result = handler(normalized)
        result._original_record = original.copy()
        return result


class GenericExecutionV1(_GenericRecordV1):
    """One v1 fill, validated independently of accounts or persistence.

    The required list identifies unconditional fields, never a round-trip
    trade. OPTION and FUTURE additionally require an explicit multiplier.
    OPTION requires expiry, strike and option_type; missing components are
    derived from a valid OCC symbol. Supplied components must match the OCC
    symbol. FUTURE expiry is optional. FUTURE and plain-symbol OPTION fills
    require instrument_id; OCC-symbol fills without order_id may omit it.

    Every order-linked fill requires instrument_id and open_close, and an
    order-linked CLOSE also requires explicit realized_pnl, including for
    a single-leg file. OPEN fills must not report realized_pnl, even zero.

    Decimal strings and JSON numbers use a dot without thousands separators,
    with at most 18 digits in total and 8 fractional places. Enum values are
    case-insensitive. Empty cells and null values are treated as absent.
    The parser additionally checks execution_id uniqueness and requires
    stable, distinct instrument_id values for distinct combo contracts.
    Keep symbol spelling and instrument_id presence/values consistent across
    exports. The import service checks order markers against persisted raw
    history under the target account's lock before writing any records.
    """

    model_config = ConfigDict(json_schema_extra=_execution_input_schema)

    execution_id: str = Field(
        min_length=SYMBOL_MIN_LENGTH,
        max_length=GENERIC_EXECUTION_ID_MAX_LENGTH,
        description=(
            "Stable, non-empty fill identifier, unique within a file and reused across exports. "
            "Idempotency is scoped to the user and generic account; re-import does not update an existing execution."
        ),
        examples=["example-equity-open", "example-spread-long"],
    )
    executed_at: AwareDatetime = Field(
        description=(
            "ISO 8601 execution timestamp with Z or an explicit UTC offset; normalized to UTC. "
            "Naive timestamps and UTC normalization overflow are rejected as executed_at field errors."
        ),
        examples=["2026-03-16T14:30:00Z", "2026-03-16T09:30:00-05:00"],
    )
    symbol: str = Field(
        min_length=SYMBOL_MIN_LENGTH,
        max_length=SYMBOL_MAX_LENGTH,
        description=(
            "Instrument ticker or full OCC option symbol. Plain-symbol OPTION requires instrument_id, expiry, strike and option_type. "
            "A valid OCC symbol fills missing contract components; supplied components must match it. Keep symbol spelling consistent across exports."
        ),
        examples=["AAPL", "AAPL  260320C00150000"],
    )
    asset_class: AssetClass = Field(
        description="Case-insensitive asset class: EQUITY, OPTION, FUTURE, FOREX or CRYPTO. OPTION/FUTURE require an explicit multiplier.",
        examples=["EQUITY", "OPTION", "FUTURE"],
    )
    side: Literal["BUY", "SELL"] = Field(
        description="Case-insensitive fill side, not the direction of the whole position: BUY or SELL.", examples=["BUY", "SELL"]
    )
    quantity: Decimal = Field(
        gt=Decimal("0"),
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        description="Positive absolute fill quantity as a decimal string or JSON number, without thousands separators.",
        examples=["10", "0.5"],
    )
    price: Decimal = Field(
        ge=Decimal("0"),
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        description="Non-negative price per unit, without the contract multiplier, as a decimal string or JSON number.",
        examples=["150.00", "0"],
    )
    currency: str = Field(
        min_length=CURRENCY_LENGTH,
        max_length=CURRENCY_LENGTH,
        pattern=r"^[A-Z]{3}$",
        description="Three uppercase ISO 4217 currency letters; unlike enum fields, lowercase currency codes are rejected.",
        examples=["USD", "EUR"],
    )
    commission: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        description="Non-negative sum of all execution fees as an absolute value; defaults to zero.",
        examples=["0", "0.65"],
    )
    open_close: Literal["OPEN", "CLOSE"] | None = Field(
        default=None,
        description=(
            "Case-insensitive position effect: OPEN or CLOSE; required for every order_id-linked fill. "
            "Must agree across the order within the file and with persisted target-account raw history, checked under the account lock. "
            "Use CLOSE for closing fills, especially pre-period positions. Without order_id, empty/null means unspecified and "
            "CLOSE without realized_pnl supplies only the FIFO zero-P&L closing marker. Order-linked CLOSE requires realized_pnl; "
            "OPEN must not report realized_pnl, including zero."
        ),
        examples=["OPEN", "CLOSE", None],
    )
    multiplier: int = Field(
        default=1,
        ge=1,
        validate_default=True,
        description=(
            "Positive integer contract multiplier, accepted as a JSON number or numeric string. "
            "Explicitly required for OPTION and FUTURE, even with an OCC symbol; defaults to 1 only for other classes."
        ),
        examples=[100, "100", 50, 1],
    )
    instrument_id: str | None = Field(
        default=None,
        max_length=GENERIC_INSTRUMENT_ID_MAX_LENGTH,
        description=(
            "Stable contract identifier, required for FUTURE, plain-symbol OPTION and every order_id-linked fill, even single-leg or OCC uploads. "
            "Without order_id, optional for ordinary EQUITY/FOREX/CRYPTO and matching OCC-symbol OPTION fills. "
            "Distinct contracts require distinct non-empty IDs, reused on opening/closing fills and across files. "
            "Keep ID presence and values consistent across exports, including when an ID is optional. "
            "Supply IDs even when combo legs are uploaded in separate files; the parser cannot infer a multi-leg order from a single contract."
        ),
        examples=["AAPL-20260320-C-150", "AAPL-20260320-C-155", None],
    )
    expiry: date_ | None = Field(
        default=None,
        validate_default=True,
        description=(
            "YYYY-MM-DD expiry. Required for OPTION unless derivable from a valid OCC symbol; supplied values must agree with that symbol. Optional for FUTURE."
        ),
        examples=["2026-03-20", None],
    )
    strike: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        validate_default=True,
        description="Positive option strike; required for OPTION unless derivable from a valid OCC symbol. Supplied values must agree with that symbol.",
        examples=["150.00", None],
    )
    option_type: Literal["C", "P"] | None = Field(
        default=None,
        validate_default=True,
        description=(
            "Case-insensitive C (call) or P (put); required for OPTION unless derivable from a valid OCC symbol. Supplied values must agree with that symbol."
        ),
        examples=["C", "P", None],
    )
    order_id: str | None = Field(
        default=None,
        description=(
            "Optional stable order identifier that opts into order/combo metadata. Distinct contracts in the group "
            "require distinct instrument_id values and matching execution timestamps for combo grouping. Every order-linked fill requires "
            "instrument_id and an explicit, consistent OPEN/CLOSE marker, even in a single-leg or OCC file; every order-linked CLOSE requires realized_pnl. "
            "Reuse the ID across exports of the same order, but use different IDs for opening and closing orders. "
            "Markers must also agree with persisted raw history for that order in the target account."
        ),
        examples=["example-spread-open", "example-spread-close", None],
    )
    realized_pnl: Decimal | None = Field(
        default=None,
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        description=(
            "Broker-reported realized P&L of a closing fill, required for every order-linked CLOSE, including single-leg partial files; "
            "a genuine breakeven close explicitly supplies 0. Must be absent/null for OPEN, including no reported zero. "
            "Otherwise optional: without order_id, CLOSE without it maps to a FIFO zero-P&L closing marker."
        ),
        examples=["20.00", "-5.50", "0", None],
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_contract_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = cast(dict[str, object], value).copy()
        asset_class = data.get("asset_class")
        normalized_class = asset_class.strip().upper() if isinstance(asset_class, str) else asset_class
        if normalized_class in (AssetClass.OPTION, AssetClass.FUTURE) and "multiplier" not in data:
            data["multiplier"] = _REQUIRED_FIELD
        symbol = data.get("symbol")
        occ_contract: tuple[Decimal | None, date_ | None, str | None] = (None, None, None)
        if normalized_class == AssetClass.OPTION and isinstance(symbol, str):
            occ_contract = parse_occ_option(symbol.strip().upper())
            strike, expiry, option_type = occ_contract
            for field, derived in (("strike", strike), ("expiry", expiry), ("option_type", option_type)):
                if field not in data and derived is not None:
                    data[field] = derived
        if normalized_class == AssetClass.OPTION:
            for field in ("expiry", "strike", "option_type"):
                if field not in data:
                    data[field] = _REQUIRED_FIELD
        needs_instrument_id = normalized_class == AssetClass.FUTURE or (normalized_class == AssetClass.OPTION and any(part is None for part in occ_contract))
        if needs_instrument_id and "instrument_id" not in data:
            data["instrument_id"] = _REQUIRED_FIELD
        order_id = data.get("order_id")
        if isinstance(order_id, str) and order_id.strip():
            for field in ("instrument_id", "open_close"):
                if field not in data:
                    data[field] = _REQUIRED_FIELD
            marker = data.get("open_close")
            if isinstance(marker, str) and marker.strip().upper() == "CLOSE" and "realized_pnl" not in data:
                data["realized_pnl"] = _REQUIRED_FIELD
        return data

    @field_validator("asset_class", "side", "open_close", "option_type", mode="before")
    @classmethod
    def _normalize_enum(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("quantity", "price", "commission", "strike", "realized_pnl", mode="before")
    @classmethod
    def _validate_decimal(cls, value: object) -> object:
        return _validate_decimal_input(value)

    @field_validator("expiry", mode="before")
    @classmethod
    def _validate_expiry(cls, value: object) -> object:
        return _validate_date_input(value)

    @field_validator("expiry", "strike", "option_type")
    @classmethod
    def _match_occ_contract(cls, value: object, info: ValidationInfo) -> object:
        symbol = info.data.get("symbol")
        if info.data.get("asset_class") == AssetClass.OPTION and isinstance(symbol, str):
            strike, expiry, option_type = parse_occ_option(symbol.upper())
            expected = {"expiry": expiry, "strike": strike, "option_type": option_type}.get(info.field_name or "")
            if expected is not None and value != expected:
                raise PydanticCustomError("occ_contract_mismatch", "Contract field must match the OCC symbol.")
        return value

    @field_validator("realized_pnl")
    @classmethod
    def _reject_open_realized_pnl(cls, value: Decimal | None, info: ValidationInfo) -> Decimal | None:
        if value is not None and info.data.get("open_close") == "OPEN":
            raise PydanticCustomError("open_realized_pnl", "OPEN fills must not report realized_pnl.")
        return value

    @field_validator("executed_at", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.strip())
            except ValueError:
                raise PydanticCustomError("datetime_parsing", "Use an ISO 8601 timestamp with a timezone offset.") from None
        raise PydanticCustomError("datetime_type", "Use an ISO 8601 timestamp with a timezone offset.")

    @field_validator("executed_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError):
            raise PydanticCustomError("datetime_utc_range", "Use a timestamp representable in UTC.") from None

    @field_validator("multiplier", mode="before")
    @classmethod
    def _reject_boolean_multiplier(cls, value: object) -> object:
        if isinstance(value, bool):
            raise PydanticCustomError("int_type", "Use a positive integer multiplier.")
        return value

    @field_validator("multiplier", "instrument_id", "expiry", "strike", "option_type", "open_close", "realized_pnl", mode="before")
    @classmethod
    def _require_conditional_field(cls, value: object) -> object:
        # A sentinel makes conditional failures ordinary field errors, collected
        # alongside invalid quantity/timestamp/etc. instead of aborting the row.
        if value is _REQUIRED_FIELD:
            raise PydanticCustomError("missing", "Field is required.")
        return value

    def to_raw(self) -> BrokerRawTransactionCreate:
        """Map this fill to the unified raw pipeline without aggregation.

        Returns:
            One raw execution retaining original fields and unknown extras
            under inert raw_data.source_record. Only validated order_id and
            derived cash populate active pipeline metadata. JSON numeric
            literals are stored losslessly as decimal strings for JSONB.
        """
        raw_data: dict[str, object] = {GENERIC_IMPORT_SOURCE_RECORD_KEY: _RAW_DATA_ADAPTER.dump_python(self._original_record, mode="json")}
        if self.order_id is not None:
            notional = self.quantity * self.price * self.multiplier
            net_cash = (notional if self.side == "SELL" else -notional) - self.commission
            raw_data[GENERIC_PIPELINE_ORDER_ID_KEY] = self.order_id
            raw_data[GENERIC_PIPELINE_NET_CASH_KEY] = str(net_cash)
        pnl = self.realized_pnl
        if pnl is None and self.open_close == "CLOSE":
            pnl = Decimal("0")
        return BrokerRawTransactionCreate(
            id=f"{GENERIC_IMPORT_BROKER}:{self.execution_id}",
            broker_type=GENERIC_IMPORT_BROKER,
            symbol=self.symbol,
            conid=self.instrument_id,
            asset_class=self.asset_class,
            direction=Direction.LONG if self.side == "BUY" else Direction.SHORT,
            quantity=self.quantity,
            price=self.price,
            currency=self.currency,
            commission=self.commission,
            multiplier=self.multiplier,
            executed_at=self.executed_at,
            pnl=pnl,
            open_close={"OPEN": "O", "CLOSE": "C"}.get(self.open_close) if self.open_close is not None else None,
            expiry=self.expiry,
            strike=self.strike,
            option_type=self.option_type,
            raw_data=raw_data,
        )


class GenericCorporateActionV1(_GenericRecordV1):
    """A JSON-only stock split with a positive ratio bounded to Numeric(18,8)."""

    model_config = ConfigDict(json_schema_extra=_corporate_action_input_schema)

    symbol: str = Field(min_length=SYMBOL_MIN_LENGTH, max_length=SYMBOL_MAX_LENGTH, description="Symbol affected by the split.", examples=["AAPL"])
    date: date_ = Field(description="Effective date in YYYY-MM-DD format.", examples=["2026-03-16"])
    ratio: Decimal = Field(
        gt=Decimal("0"),
        max_digits=NUMERIC_PRECISION,
        decimal_places=NUMERIC_SCALE,
        description=(
            "Finite, positive split ratio as after-shares divided by before-shares, as a decimal string or JSON number. "
            "Must fit Numeric(18,8): at most 18 digits and 8 fractional places. Positive subscale values are rejected, not rounded to zero."
        ),
        examples=["4", "0.05", "0.00000001"],
    )
    old_instrument_id: str | None = Field(
        default=None,
        max_length=GENERIC_INSTRUMENT_ID_MAX_LENGTH,
        description="Optional contract identifier before the split.",
        examples=["example-equity-before-split", None],
    )
    new_instrument_id: str | None = Field(
        default=None,
        max_length=GENERIC_INSTRUMENT_ID_MAX_LENGTH,
        description="Optional contract identifier after the split.",
        examples=["example-equity-after-split", None],
    )

    @field_validator("ratio", mode="before")
    @classmethod
    def _validate_ratio(cls, value: object) -> object:
        return _validate_decimal_input(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return _validate_date_input(value)

    def to_raw(self) -> CorporateActionCreateRequest:
        """Map this split to the same corporate-action DTO as native parsers.

        Returns:
            The split request consumed by the unified import service.
        """
        return CorporateActionCreateRequest(
            symbol=self.symbol,
            date=self.date,
            ratio=self.ratio,
            old_conid=self.old_instrument_id,
            new_conid=self.new_instrument_id,
        )


class GenericImportDocumentV1(BaseModel):
    """The versioned JSON document accepted by generic execution import.

    Each execution is one fill, not a round-trip trade. Validation is
    all-or-nothing, including unique execution_id values within the file
    and stable instrument_id values for FUTURE, plain-symbol OPTION and all
    order-linked fills. Standalone valid OCC-symbol options without order_id
    may omit instrument_id. Order-linked fills require consistent OPEN/CLOSE
    markers and CLOSE requires reported realized_pnl; OPEN must not report P&L,
    even zero.
    The import service checks order markers against persisted target-account
    raw history under its lock. Original execution records and unknown fields
    remain inert in raw_data.source_record; only validated fields drive
    active order metadata and derived cash.
    Numeric literals are decoded losslessly; decimal strings are also accepted.
    Corporate actions are supported only in JSON, not in the CSV encoding.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(
        pattern=GENERIC_IMPORT_SCHEMA_VERSION_PATTERN,
        description="Required version string: '1' or any string starting with '1.'. Numeric versions and other major versions are rejected.",
        examples=["1", "1.0"],
    )
    executions: list[GenericExecutionV1] = Field(
        min_length=1,
        description=(
            "Required, non-empty array containing one record per fill. execution_id must be unique within the file; all records validate before persistence."
        ),
        examples=[
            [
                {
                    "execution_id": "example-equity-open",
                    "executed_at": "2026-03-16T14:30:00Z",
                    "symbol": "AAPL",
                    "asset_class": "EQUITY",
                    "side": "BUY",
                    "quantity": "10",
                    "price": "150.00",
                    "currency": "USD",
                }
            ]
        ],
    )
    corporate_actions: list[GenericCorporateActionV1] = Field(
        default_factory=list,
        description="Optional stock splits, supported in JSON only; omitted means an empty array.",
        examples=[[], [{"symbol": "AAPL", "date": "2026-03-16", "ratio": "4"}]],
    )
