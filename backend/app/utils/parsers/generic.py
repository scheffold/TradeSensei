"""Collect-all, execution-only adapters for generic v1 CSV and JSON files."""

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError

from app.core.exceptions import GenericImportValidationError, GenericRowError, ImportParseError
from app.models.trade import AssetClass
from app.schemas.broker_raw_transaction import BrokerRawTransactionCreate
from app.schemas.corporate_action import CorporateActionCreateRequest
from app.schemas.generic_import import GenericCorporateActionV1, GenericExecutionV1, GenericImportDocumentV1
from app.schemas.trade import TradeCreate
from app.utils.occ import parse_occ_option
from app.utils.parsers.aggregator import CorporateActionLike
from app.utils.parsers.base import BrokerParser

_FIELD_NAMES = frozenset(GenericExecutionV1.model_fields) | frozenset(GenericCorporateActionV1.model_fields) | frozenset(GenericImportDocumentV1.model_fields)
_ContractDescription = tuple[str, AssetClass, date | None, Decimal | None, Literal["C", "P"] | None]
_CONTRACT_ADAPTER: TypeAdapter[_ContractDescription] = TypeAdapter(_ContractDescription, config=ConfigDict(allow_inf_nan=False))


def _safe_validation_message(field: str, error_type: str) -> str:
    """Describe a failed rule without using Pydantic messages, inputs or context."""
    if field == "schema_version":
        return "schema_version must be '1' or start with '1.'."
    if field == "executions" and error_type == "too_short":
        return "The file contains no executions."
    if error_type == "timezone_aware":
        return f"{field} requires an explicit timezone offset or Z."
    if error_type == "datetime_utc_range":
        return f"{field} must be representable as a UTC timestamp."
    if field == "currency" and error_type == "string_pattern_mismatch":
        return "currency must contain three uppercase letters."
    messages = {
        "missing": "is required.",
        "string_type": "must be a string.",
        "string_too_short": "must not be empty.",
        "string_too_long": "exceeds the maximum supported length.",
        "enum": "contains an unsupported enumeration value.",
        "literal_error": "contains an unsupported enumeration value.",
        "occ_contract_mismatch": "must match the OCC symbol.",
        "open_realized_pnl": "must be omitted for OPEN fills, including reported zero.",
        "greater_than": "must be greater than zero.",
        "greater_than_equal": "is below the allowed minimum.",
        "decimal_type": "must be a decimal number.",
        "decimal_parsing": "must be a decimal without thousands separators.",
        "decimal_max_digits": "exceeds the supported decimal precision.",
        "decimal_max_places": "exceeds the supported decimal scale.",
        "decimal_whole_digits": "exceeds the supported decimal precision.",
        "finite_number": "must be finite.",
        "int_type": "must be an integer.",
        "int_parsing": "must be an integer.",
        "int_from_float": "must be an integer.",
        "list_type": "must be an array.",
        "model_type": "must be an object.",
    }
    if error_type.startswith("datetime"):
        return f"{field} must be an ISO 8601 timestamp with a timezone offset."
    if error_type.startswith("date"):
        return f"{field} must be a valid YYYY-MM-DD date."
    return f"{field} {messages.get(error_type, 'is invalid.')}"


def _row_errors(exc: ValidationError, location: int | str = "document") -> list[GenericRowError]:
    errors: list[GenericRowError] = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        path = error["loc"]
        row_location = location
        if len(path) >= 2 and path[0] in ("executions", "corporate_actions") and isinstance(path[1], int):
            row_location = f"{path[0]}[{path[1]}]"
        field = str(path[-1]) if path and path[-1] in _FIELD_NAMES else "record"
        errors.append(GenericRowError(row_location, field, _safe_validation_message(field, error["type"])))
    return errors


def _duplicate_errors(rows: list[tuple[int | str, object]]) -> list[GenericRowError]:
    locations: dict[str, list[int | str]] = {}
    for location, record in rows:
        if not isinstance(record, dict):
            continue
        execution_id = record.get("execution_id")
        if isinstance(execution_id, str) and execution_id.strip():
            locations.setdefault(execution_id.strip(), []).append(location)
    return [
        GenericRowError(location, "execution_id", "execution_id must be unique within the file.")
        for duplicate_locations in locations.values()
        if len(duplicate_locations) > 1
        for location in duplicate_locations
    ]


def _combo_instrument_errors(rows: list[tuple[int | str, object]]) -> list[GenericRowError]:
    """Check contract identities even when unrelated execution fields are invalid."""
    groups: dict[str, list[tuple[int | str, _ContractDescription, str | None]]] = {}
    for location, record in rows:
        if not isinstance(record, dict):
            continue
        order_id = record.get("order_id")
        symbol = record.get("symbol")
        asset_class = record.get("asset_class")
        if not isinstance(order_id, str) or not order_id.strip() or not isinstance(symbol, str) or not isinstance(asset_class, str):
            continue
        # Normalize only the contract comparison key, independently of fields
        # such as quantity/execution_id whose errors must not hide combo errors.
        contract = [record.get(field) for field in ("expiry", "strike", "option_type")]
        contract = [value.strip() if isinstance(value, str) else value for value in contract]
        contract = [None if value == "" else value for value in contract]
        normalized_class = asset_class.strip().upper()
        if normalized_class == AssetClass.OPTION:
            strike, expiry, option_type = parse_occ_option(symbol.strip().upper())
            contract = [derived if value is None else value for value, derived in zip(contract, (expiry, strike, option_type), strict=True)]
        if isinstance(contract[2], str):
            contract[2] = contract[2].upper()
        try:
            description = _CONTRACT_ADAPTER.validate_python((symbol.strip(), normalized_class, *contract))
        except ValidationError:
            # The normal record validation reports invalid contract fields.
            continue
        instrument_id = record.get("instrument_id")
        identifier = instrument_id.strip() if isinstance(instrument_id, str) and instrument_id.strip() else None
        groups.setdefault(order_id.strip(), []).append((location, description, identifier))

    errors: list[GenericRowError] = []
    for group in groups.values():
        if len({description for _, description, _ in group}) < 2:
            continue
        by_identifier: dict[str, list[tuple[int | str, _ContractDescription]]] = {}
        for location, description, identifier in group:
            if identifier is None:
                errors.append(GenericRowError(location, "instrument_id", "instrument_id is required for each distinct contract in a multi-leg order."))
            else:
                by_identifier.setdefault(identifier, []).append((location, description))
        for contracts in by_identifier.values():
            if len({description for _, description in contracts}) > 1:
                errors.extend(
                    GenericRowError(location, "instrument_id", "instrument_id must not identify different contracts in one order.") for location, _ in contracts
                )
    return errors


def _order_marker_errors(rows: list[tuple[int | str, object]]) -> list[GenericRowError]:
    """Check shared-order markers even when unrelated row fields are invalid."""
    groups: dict[str, list[tuple[int | str, str]]] = {}
    for location, record in rows:
        if not isinstance(record, dict):
            continue
        order_id = record.get("order_id")
        marker = record.get("open_close")
        if isinstance(order_id, str) and order_id.strip() and isinstance(marker, str) and marker.strip().upper() in ("OPEN", "CLOSE"):
            groups.setdefault(order_id.strip(), []).append((location, marker.strip().upper()))
    return [
        GenericRowError(location, "open_close", "open_close must be consistent across records sharing order_id.")
        for group in groups.values()
        if len({marker for _, marker in group}) > 1
        for location, _ in group
    ]


class GenericParser(BrokerParser):
    """Validate complete v1 documents before emitting any raw executions.

    This parser does not aggregate, persist or deduplicate records. Duplicate
    IDs inside a file are validation errors, including on otherwise-invalid
    rows. Re-import idempotency remains the unified pipeline's responsibility.

    Attributes:
        warnings: Non-fatal warnings for the current file.
        execution_locations: Pre-stamped raw IDs mapped to CSV physical lines
            or JSON execution paths after successful validation. Reset per parse.
    """

    def __init__(self) -> None:
        """Initialise per-file warnings and execution locations."""
        self.warnings: list[str] = []
        self.execution_locations: dict[str, int | str] = {}

    def parse(
        self,
        content: bytes,
        filename: str,
        corporate_actions: Sequence[CorporateActionLike] | None = None,
    ) -> tuple[list[TradeCreate], list[object]]:
        """Reject the legacy trade-level parsing path.

        Raises:
            NotImplementedError: Generic import supports parse_raw() only.
        """
        raise NotImplementedError("GenericParser only supports parse_raw().")

    def parse_raw(
        self,
        content: bytes,
        filename: str,
    ) -> tuple[list[BrokerRawTransactionCreate], list[CorporateActionCreateRequest]]:
        """Detect the encoding and validate all executions and corporate actions.

        Args:
            content: UTF-8 CSV or JSON document, optionally with a BOM.
            filename: Original filename; deliberately unused for detection.

        Returns:
            Raw executions and corporate actions for ImportService.

        Raises:
            GenericImportValidationError: Any document or row validation failed.
            ImportParseError: XML content was provided instead of CSV/JSON.
        """
        self.warnings = []
        self.execution_locations = {}
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise GenericImportValidationError([GenericRowError("document", "encoding", "The file must use UTF-8 encoding.")]) from None
        first_character = text.lstrip()[:1]
        if first_character == "<":
            raise ImportParseError("Generic import supports CSV and JSON only; XML is not supported.")
        if first_character == "{":
            return self._parse_json(text)
        return self._parse_csv(text)

    def _parse_json(self, text: str) -> tuple[list[BrokerRawTransactionCreate], list[CorporateActionCreateRequest]]:
        try:
            payload: object = json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=Decimal)
        except (json.JSONDecodeError, ValueError, RecursionError):
            raise GenericImportValidationError([GenericRowError("document", "document", "The document contains invalid JSON.")]) from None

        rows: list[tuple[int | str, object]] = []
        if isinstance(payload, dict):
            executions = payload.get("executions")
            if isinstance(executions, list):
                rows = [(f"executions[{index}]", record) for index, record in enumerate(executions)]
        errors = _duplicate_errors(rows) + _combo_instrument_errors(rows) + _order_marker_errors(rows)
        try:
            document = GenericImportDocumentV1.model_validate(payload)
        except ValidationError as exc:
            errors.extend(_row_errors(exc))
            raise GenericImportValidationError(errors) from None
        if errors:
            raise GenericImportValidationError(errors)
        raw_records = [execution.to_raw() for execution in document.executions]
        self.execution_locations = {record.id: f"executions[{index}]" for index, record in enumerate(raw_records)}
        return raw_records, [action.to_raw() for action in document.corporate_actions]

    def _parse_csv(self, text: str) -> tuple[list[BrokerRawTransactionCreate], list[CorporateActionCreateRequest]]:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        errors: list[GenericRowError] = []
        try:
            headers = reader.fieldnames or []
        except csv.Error:
            raise GenericImportValidationError([GenericRowError(1, "header", "The CSV header is invalid.")]) from None

        fields = GenericExecutionV1.model_fields
        header_counts = Counter(headers)
        for field, info in fields.items():
            if info.is_required() and field not in header_counts:
                errors.append(GenericRowError(1, field, f"Required CSV column {field} is missing."))
        for field, count in header_counts.items():
            if count > 1:
                safe_field = field if field in fields else "header"
                errors.append(GenericRowError(1, safe_field, "CSV column names must be unique."))
            if field not in fields:
                # Only echo syntactically valid column names, never arbitrary
                # content that could be a misplaced execution value.
                safe_name = field if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", field) else "unrecognized column"
                self.warnings.append(f"Unknown CSV column ignored: {safe_name}.")

        rows: list[tuple[int | str, object]] = []
        raw_records: list[BrokerRawTransactionCreate] = []
        try:
            for csv_row in reader:
                location = reader.line_num
                record: dict[str, object] = {field: value for field, value in csv_row.items() if field is not None}
                rows.append((location, record))
                if None in csv_row:
                    errors.append(GenericRowError(location, "record", "The CSV row has more cells than the header."))
                try:
                    raw_records.append(GenericExecutionV1.model_validate(record).to_raw())
                except ValidationError as exc:
                    errors.extend(_row_errors(exc, location))
        except csv.Error:
            errors.append(GenericRowError(reader.line_num, "record", "The CSV row contains invalid quoting or field structure."))

        errors.extend(_duplicate_errors(rows))
        errors.extend(_combo_instrument_errors(rows))
        errors.extend(_order_marker_errors(rows))
        if not rows:
            errors.append(GenericRowError(1, "executions", "The file contains no executions."))
        if errors:
            raise GenericImportValidationError(errors)
        self.execution_locations = {record.id: location for record, (location, _) in zip(raw_records, rows, strict=True)}
        return raw_records, []
