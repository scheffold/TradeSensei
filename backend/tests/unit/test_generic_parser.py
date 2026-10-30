"""Spec-driven generic v1 validation; no database or import service is involved."""

import csv
import io
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from app.core.exceptions import GenericImportValidationError, ImportParseError
from app.models.trade import AssetClass, Direction
from app.schemas.broker_raw_transaction import BrokerRawTransactionCreate
from app.schemas.generic_import import GenericCorporateActionV1, GenericExecutionV1
from app.utils.parsers.factory import BrokerType, get_parser
from app.utils.parsers.generic import GenericParser

pytestmark = pytest.mark.unit

_REQUIRED_FIELDS = ("execution_id", "executed_at", "symbol", "asset_class", "side", "quantity", "price", "currency")
_V1_FIELDS = (
    *_REQUIRED_FIELDS,
    "commission",
    "open_close",
    "multiplier",
    "instrument_id",
    "expiry",
    "strike",
    "option_type",
    "order_id",
    "realized_pnl",
)
_ENCODINGS = ("csv", "json")


def _execution(**changes: object) -> dict[str, object]:
    return {
        "execution_id": "fill-1",
        "executed_at": "2026-03-14T10:31:07Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": "2",
        "price": "123.45",
        "currency": "USD",
        **changes,
    }


def _option(**changes: object) -> dict[str, object]:
    return _execution(
        **{
            "asset_class": "OPTION",
            "multiplier": "100",
            "expiry": "2026-06-19",
            "strike": "150",
            "option_type": "C",
            "instrument_id": "call-150",
            "open_close": "OPEN",
            **changes,
        }
    )


def _csv(rows: Sequence[dict[str, object]], fields: Sequence[str] = _V1_FIELDS) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _content(rows: Sequence[dict[str, object]], encoding: str, **document_fields: object) -> bytes:
    if encoding == "csv":
        fields = dict.fromkeys((*_V1_FIELDS, *(field for row in rows for field in row)))
        return _csv(rows, tuple(fields))
    return json.dumps({"schema_version": "1.0", "executions": rows, **document_fields}, default=str).encode("utf-8")


def _invalid(content: bytes, filename: str = "fills.csv") -> GenericImportValidationError:
    with pytest.raises(GenericImportValidationError) as caught:
        GenericParser().parse_raw(content, filename)
    assert caught.value.errors
    assert all(set(asdict(error)) == {"location", "field", "message"} for error in caught.value.errors)
    assert all(error.message for error in caught.value.errors)
    return caught.value


def _locations(error: GenericImportValidationError, field: str) -> set[int | str]:
    return {entry.location for entry in error.errors if entry.field == field}


def _source_record(record: BrokerRawTransactionCreate) -> dict[str, object]:
    source = record.raw_data["source_record"]
    assert isinstance(source, dict)
    return cast(dict[str, object], source)


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_minimal_equity_accepts_defaults_and_maps_one_fill(encoding: str) -> None:
    original = _execution()
    content = _csv([original], _REQUIRED_FIELDS) if encoding == "csv" else _content([original], encoding)
    parser = GenericParser()

    records, actions = parser.parse_raw(content, f"fills.{encoding}")

    assert len(records) == 1
    record = records[0]
    assert record.id == "generic:fill-1"
    assert record.broker_type == "generic"
    assert record.symbol == "AAPL"
    assert record.asset_class == AssetClass.EQUITY
    assert record.direction == Direction.LONG
    assert record.quantity == Decimal("2")
    assert record.price == Decimal("123.45")
    assert record.commission == Decimal("0")
    assert record.multiplier == 1
    assert record.currency == "USD"
    assert record.conid is None
    assert record.open_close is None
    assert record.pnl is None
    assert record.executed_at == datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC)
    assert record.raw_data == {"source_record": original}
    assert actions == []
    assert parser.warnings == []


def test_generic_execution_schema_v1_field_list_matches_the_public_contract() -> None:
    assert tuple(GenericExecutionV1.model_fields) == _V1_FIELDS
    assert {field for field, info in GenericExecutionV1.model_fields.items() if info.is_required()} == set(_REQUIRED_FIELDS)


@pytest.mark.parametrize("asset_class", ["EQUITY", "FOREX", "CRYPTO"])
@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_non_derivative_fill_without_identifier_accepts_unit_multiplier(asset_class: str, encoding: str) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(asset_class=asset_class)], encoding), "fills.csv")
    assert records[0].asset_class == asset_class
    assert records[0].multiplier == 1
    assert records[0].conid is None


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_explicit_option_contract_preserves_contract_fields(encoding: str) -> None:
    record = _option(instrument_id="contract-call", order_id="order-77", open_close="OPEN", commission="0.65")
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    raw = records[0]
    assert raw.asset_class == AssetClass.OPTION
    assert raw.multiplier == 100
    assert raw.expiry == date(2026, 6, 19)
    assert raw.strike == Decimal("150")
    assert raw.option_type == "C"
    assert raw.conid == "contract-call"
    assert raw.open_close == "O"
    assert raw.raw_data["brokerageOrderID"] == "order-77"
    assert Decimal(str(raw.raw_data["netCash"])) == Decimal("-24690.65")


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("option_type", ["C", "P"])
def test_parse_raw_occ_option_without_explicit_fields_derives_contract(encoding: str, option_type: str) -> None:
    record = _execution(asset_class="OPTION", symbol=f"AAPL  260619{option_type}00150000", multiplier="100")
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].expiry == date(2026, 6, 19)
    assert records[0].strike == Decimal("150")
    assert records[0].option_type == option_type
    assert _source_record(records[0])["symbol"] == record["symbol"]


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_occ_option_with_matching_explicit_field_derives_other_components(encoding: str) -> None:
    record = _execution(asset_class="OPTION", symbol="AAPL  260619C00150000", multiplier="100", strike="150")
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].strike == Decimal("150")
    assert records[0].expiry == date(2026, 6, 19)
    assert records[0].option_type == "C"


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("missing_fields", [("expiry",), ("strike",), ("option_type",), ("expiry", "strike", "option_type")])
def test_parse_raw_non_occ_option_missing_contract_fields_reports_every_missing_field(encoding: str, missing_fields: tuple[str, ...]) -> None:
    record = _option()
    for field in missing_fields:
        record.pop(field)
    error = _invalid(_content([record], encoding))
    assert {entry.field for entry in error.errors} == set(missing_fields)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("asset_class", ["OPTION", "FUTURE"])
@pytest.mark.parametrize("absent", [None, "", "  "])
def test_parse_raw_derivative_missing_multiplier_reports_required_field(encoding: str, asset_class: str, absent: object) -> None:
    record = _option() if asset_class == "OPTION" else _execution(asset_class=asset_class, instrument_id="future-contract")
    record["multiplier"] = absent
    error = _invalid(_content([record], encoding))
    assert {entry.field for entry in error.errors} == {"multiplier"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_derivative_without_any_contract_fields_collects_all_errors(encoding: str) -> None:
    error = _invalid(_content([_execution(asset_class="OPTION")], encoding))
    assert {entry.field for entry in error.errors} == {"multiplier", "instrument_id", "expiry", "strike", "option_type"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("expiry", [None, "2026-06-19"])
def test_parse_raw_future_with_multiplier_accepts_optional_expiry(encoding: str, expiry: object) -> None:
    record = _execution(asset_class="FUTURE", multiplier="50", instrument_id="future-contract", expiry=expiry)
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].multiplier == 50
    assert records[0].expiry == (date(2026, 6, 19) if expiry else None)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("asset_class", ["equity", "oPtIoN", "future", "forex", "crypto"])
def test_parse_raw_mixed_case_enums_normalizes_supported_values(encoding: str, asset_class: str) -> None:
    record = _execution(
        asset_class=asset_class,
        side="sElL",
        open_close="cLoSe",
        multiplier="100",
        expiry="2026-06-19",
        strike="150",
        option_type="p",
        instrument_id="contract-p",
    )
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].asset_class == asset_class.upper()
    assert records[0].direction == Direction.SHORT
    assert records[0].open_close == "C"
    assert records[0].option_type == "P"
    assert records[0].pnl == Decimal("0")
    assert _source_record(records[0])["side"] == "sElL"


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("field", _REQUIRED_FIELDS)
@pytest.mark.parametrize("absent", ["missing", None, "", "  "])
def test_parse_raw_required_field_absent_reports_field_and_record_location(encoding: str, field: str, absent: object) -> None:
    record = _execution()
    if absent == "missing":
        record.pop(field)
    else:
        record[field] = absent
    error = _invalid(_content([record], encoding))
    assert _locations(error, field) == ({2} if encoding == "csv" else {"executions[0]"})


@pytest.mark.parametrize("field", _REQUIRED_FIELDS)
def test_parse_raw_csv_missing_required_column_reports_header_field(field: str) -> None:
    error = _invalid(_csv([_execution()], [name for name in _V1_FIELDS if name != field]))
    assert 1 in _locations(error, field)
    assert any("column" in entry.message.lower() for entry in error.errors if entry.location == 1 and entry.field == field)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_id", "x" * 101),
        ("asset_class", "BOND"),
        ("side", "SHORT"),
        ("open_close", "C;O"),
        ("quantity", "0"),
        ("quantity", "-1"),
        ("quantity", "1,000.25"),
        ("quantity", "1,25"),
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("quantity", "-Infinity"),
        ("price", "-0.01"),
        ("price", "1,234.50"),
        ("price", "12,50"),
        ("price", "NaN"),
        ("price", "Infinity"),
        ("price", "1.000000001"),
        ("price", "10000000000"),
        ("commission", "-0.01"),
        ("commission", "1,000"),
        ("commission", "NaN"),
        ("realized_pnl", "1,000.25"),
        ("realized_pnl", "NaN"),
        ("realized_pnl", "Infinity"),
        ("currency", "usd"),
        ("currency", "US"),
        ("currency", "USDD"),
        ("currency", "U$D"),
        ("multiplier", "0"),
        ("multiplier", "-1"),
        ("multiplier", "1.5"),
        ("multiplier", "1,000"),
        ("instrument_id", "c" * 51),
        ("expiry", "20260619"),
        ("expiry", "06/19/2026"),
        ("expiry", "2026-02-30"),
        ("expiry", "2026-06-19T00:00:00Z"),
        ("strike", "0"),
        ("strike", "-150"),
        ("strike", "1,000"),
        ("strike", "NaN"),
        ("option_type", "CALL"),
    ],
)
def test_parse_raw_invalid_v1_field_rejects_the_document_with_safe_field_error(encoding: str, field: str, value: object) -> None:
    error = _invalid(_content([_execution(**{field: value})], encoding))
    assert _locations(error, field) == ({2} if encoding == "csv" else {"executions[0]"})


@pytest.mark.parametrize("field", ["execution_id", "symbol", "asset_class", "side", "currency", "instrument_id", "order_id"])
@pytest.mark.parametrize("value", [True, 123, [], {}])
def test_parse_raw_json_non_string_identifier_or_enum_reports_field(field: str, value: object) -> None:
    error = _invalid(_content([_execution(**{field: value})], "json"))
    assert _locations(error, field) == {"executions[0]"}


@pytest.mark.parametrize("field", ["quantity", "price", "commission", "strike", "realized_pnl", "multiplier"])
def test_parse_raw_json_boolean_numeric_field_is_not_accepted_as_one(field: str) -> None:
    error = _invalid(_content([_execution(**{field: True})], "json"))
    assert _locations(error, field) == {"executions[0]"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_identifier_length_boundaries_accepts_longest_supported_values(encoding: str) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(execution_id="e" * 100, instrument_id="i" * 50)], encoding), "fills.csv")
    assert records[0].id == f"generic:{'e' * 100}"
    assert records[0].conid == "i" * 50


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("otherwise_invalid", [False, True])
def test_parse_raw_duplicate_identifier_reports_both_locations_even_if_one_row_is_invalid(encoding: str, otherwise_invalid: bool) -> None:
    rows = [_execution(), _execution(execution_id=" fill-1 ", quantity="0" if otherwise_invalid else "3")]
    error = _invalid(_content(rows, encoding))
    assert _locations(error, "execution_id") == ({2, 3} if encoding == "csv" else {"executions[0]", "executions[1]"})
    if otherwise_invalid:
        assert _locations(error, "quantity") == ({3} if encoding == "csv" else {"executions[1]"})
    assert "fill-1" not in str([asdict(entry) for entry in error.errors])


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_three_duplicate_identifiers_reports_every_occurrence(encoding: str) -> None:
    error = _invalid(_content([_execution(), _execution(), _execution()], encoding))
    assert _locations(error, "execution_id") == ({2, 3, 4} if encoding == "csv" else {"executions[0]", "executions[1]", "executions[2]"})


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_distinct_execution_ids_on_identical_fills_emits_both_without_aggregation(encoding: str) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(), _execution(execution_id="fill-2")], encoding), "fills.csv")
    assert len(records) == 2
    assert {record.id for record in records} == {"generic:fill-1", "generic:fill-2"}
    assert all(record.quantity == Decimal("2") for record in records)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-03-14T10:31:07-05:00", datetime(2026, 3, 14, 15, 31, 7, tzinfo=UTC)),
        ("2026-03-14T10:31:07+05:30", datetime(2026, 3, 14, 5, 1, 7, tzinfo=UTC)),
        ("2026-03-14T10:31:07.123456Z", datetime(2026, 3, 14, 10, 31, 7, 123456, tzinfo=UTC)),
    ],
)
def test_parse_raw_timezone_aware_timestamp_normalizes_to_utc(encoding: str, timestamp: str, expected: datetime) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(executed_at=timestamp)], encoding), "fills.csv")
    assert records[0].executed_at == expected
    assert records[0].executed_at.tzinfo == UTC


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("timestamp", ["2026-03-14T10:31:07", "2026-03-14", "2026-02-30T10:31:07Z", "not-a-date", "1773484267"])
def test_parse_raw_naive_or_invalid_timestamp_requires_explicit_timezone(encoding: str, timestamp: str) -> None:
    error = _invalid(_content([_execution(executed_at=timestamp)], encoding))
    assert _locations(error, "executed_at")
    assert all("timezone" in entry.message.lower() for entry in error.errors if entry.field == "executed_at")


@pytest.mark.parametrize("timestamp", [1773484267, True, [], {}])
def test_parse_raw_json_non_iso_timestamp_reports_timestamp_field(timestamp: object) -> None:
    error = _invalid(_content([_execution(executed_at=timestamp)], "json"))
    assert _locations(error, "executed_at") == {"executions[0]"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize(
    ("changes", "pnl", "open_close"),
    [
        ({}, None, None),
        ({"open_close": "OPEN"}, None, "O"),
        ({"open_close": "CLOSE"}, Decimal("0"), "C"),
        ({"open_close": "CLOSE", "realized_pnl": "-12.345"}, Decimal("-12.345"), "C"),
        ({"realized_pnl": "0"}, Decimal("0"), None),
        ({"realized_pnl": "20.5"}, Decimal("20.5"), None),
    ],
)
def test_parse_raw_closing_semantics_maps_reported_pnl_or_zero_marker(
    encoding: str, changes: dict[str, object], pnl: Decimal | None, open_close: str | None
) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(**changes)], encoding), "fills.csv")
    assert records[0].pnl == pnl
    assert records[0].open_close == open_close


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize(("side", "expected"), [("BUY", Decimal("-250.75")), ("SELL", Decimal("249.25"))])
def test_parse_raw_order_cash_metadata_uses_side_multiplier_and_absolute_fees(encoding: str, side: str, expected: Decimal) -> None:
    record = _execution(
        side=side, quantity="2", price="1.25", multiplier="100", commission="0.75", order_id="ord-77", instrument_id="equity-contract", open_close="OPEN"
    )
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].raw_data["brokerageOrderID"] == "ord-77"
    assert Decimal(str(records[0].raw_data["netCash"])) == expected
    assert _source_record(records[0])["price"] == "1.25"


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_empty_optional_cells_use_defaults_without_combo_metadata(encoding: str) -> None:
    record = _execution(**{field: "" for field in _V1_FIELDS if field not in _REQUIRED_FIELDS})
    records, _ = GenericParser().parse_raw(_content([record], encoding), "fills.csv")
    assert records[0].multiplier == 1
    assert records[0].commission == Decimal("0")
    assert records[0].conid is None
    assert records[0].pnl is None
    assert records[0].expiry is None
    assert "brokerageOrderID" not in records[0].raw_data
    assert "netCash" not in records[0].raw_data


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("value", ["0", "0.00000001"])
def test_parse_raw_zero_price_and_non_negative_fees_accepts_valid_boundary(encoding: str, value: str) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution(price=value, commission=value)], encoding), "fills.csv")
    assert records[0].price == Decimal(value)
    assert records[0].commission == Decimal(value)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("field", ["symbol", "asset_class", "expiry", "strike", "option_type"])
def test_parse_raw_multileg_contract_difference_without_identifiers_reports_each_missing_leg(encoding: str, field: str) -> None:
    first = _option(order_id="ord-77", instrument_id=None)
    values = {"symbol": "MSFT", "asset_class": "FUTURE", "expiry": "2026-07-17", "strike": "155", "option_type": "P"}
    second = {**first, "execution_id": "fill-2", field: values[field]}
    error = _invalid(_content([first, second], encoding))
    assert _locations(error, "instrument_id") == ({2, 3} if encoding == "csv" else {"executions[0]", "executions[1]"})


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("otherwise_invalid", [False, True])
def test_parse_raw_multileg_one_missing_identifier_reports_affected_leg_even_if_invalid(encoding: str, otherwise_invalid: bool) -> None:
    first = _option(order_id="ord-77", instrument_id="call-150")
    second = {**first, "execution_id": "fill-2", "strike": "155", "instrument_id": " ", "quantity": "0" if otherwise_invalid else "2"}
    error = _invalid(_content([first, second], encoding))
    assert _locations(error, "instrument_id") == ({3} if encoding == "csv" else {"executions[1]"})
    if otherwise_invalid:
        assert _locations(error, "quantity") == ({3} if encoding == "csv" else {"executions[1]"})


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_multileg_reused_identifier_reports_all_conflicting_occurrences(encoding: str) -> None:
    first = _option(order_id="ord-77", instrument_id="same-contract")
    rows = [first, {**first, "execution_id": "fill-2", "strike": "155"}, {**first, "execution_id": "fill-3"}]
    error = _invalid(_content(rows, encoding))
    assert _locations(error, "instrument_id") == ({2, 3, 4} if encoding == "csv" else {"executions[0]", "executions[1]", "executions[2]"})
    assert "same-contract" not in str(error.errors)


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_multileg_distinct_identifiers_preserves_unaggregated_legs(encoding: str) -> None:
    first = _option(order_id="ord-77", instrument_id="call-150", open_close="OPEN")
    second = {**first, "execution_id": "fill-2", "strike": "155", "instrument_id": "call-155", "side": "SELL"}
    records, _ = GenericParser().parse_raw(_content([first, second], encoding), "fills.csv")
    assert len(records) == 2
    assert {record.conid for record in records} == {"call-150", "call-155"}
    assert all(record.raw_data["brokerageOrderID"] == "ord-77" for record in records)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("instrument_id", ["call-150", "alternate-stable-id"])
def test_parse_raw_repeated_order_fills_of_same_contract_accepts_shared_identifier(encoding: str, instrument_id: str) -> None:
    first = _option(order_id="ord-77", instrument_id=instrument_id)
    records, _ = GenericParser().parse_raw(_content([first, {**first, "execution_id": "fill-2", "quantity": "1"}], encoding), "fills.csv")
    assert len(records) == 2
    assert all(record.conid == instrument_id for record in records)


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_different_orders_accept_distinct_contract_identifiers(encoding: str) -> None:
    first = _option(order_id="ord-77")
    second = {**first, "execution_id": "fill-2", "order_id": "ord-78", "strike": "155", "instrument_id": "call-155"}
    records, _ = GenericParser().parse_raw(_content([first, second], encoding), "fills.csv")
    assert len(records) == 2
    assert {record.conid for record in records} == {"call-150", "call-155"}


def test_parse_raw_csv_bom_arbitrary_column_order_and_rfc4180_quoting_accepts_fill() -> None:
    record = _execution(symbol="ACME, Inc.", order_id='order "quoted"\nsecond line', instrument_id="acme-equity", open_close="OPEN")
    content = b"\xef\xbb\xbf" + _csv([record], tuple(reversed(_V1_FIELDS)))
    records, _ = GenericParser().parse_raw(content, "excel.csv")
    assert records[0].symbol == "ACME, Inc."
    assert records[0].raw_data["brokerageOrderID"] == 'order "quoted"\nsecond line'


def test_parse_raw_csv_unknown_column_warns_once_not_once_per_row_and_resets_per_file() -> None:
    rows = [_execution(execution_id=f"fill-{index}", broker_note="private-value") for index in range(3)]
    parser = GenericParser()
    records, _ = parser.parse_raw(_csv(rows, (*_V1_FIELDS, "broker_note")), "fills.csv")
    assert len(records) == 3
    assert len(parser.warnings) == 1
    assert "broker_note" in parser.warnings[0]
    assert "private-value" not in parser.warnings[0]
    parser.parse_raw(_csv([_execution()]), "clean.csv")
    assert parser.warnings == []


def test_parse_raw_csv_corporate_action_columns_are_ignored_and_never_emit_a_split() -> None:
    record = _execution(date="2026-03-13", ratio="4", old_instrument_id="before", new_instrument_id="after")
    parser = GenericParser()
    records, actions = parser.parse_raw(_csv([record], (*_V1_FIELDS, "date", "ratio", "old_instrument_id", "new_instrument_id")), "fills.csv")
    assert len(records) == 1
    assert actions == []
    assert len(parser.warnings) == 4
    for field in ("date", "ratio", "old_instrument_id", "new_instrument_id"):
        assert sum(field in warning for warning in parser.warnings) == 1


@pytest.mark.parametrize("content", [b"", _csv([]), _csv([]) + b"\r\n"])
def test_parse_raw_empty_or_header_only_csv_reports_no_executions(content: bytes) -> None:
    error = _invalid(content)
    assert any("no executions" in entry.message.lower() for entry in error.errors)


def test_parse_raw_json_empty_execution_array_reports_no_executions() -> None:
    error = _invalid(_content([], "json"))
    assert _locations(error, "executions") == {"document"}
    assert any("no executions" in entry.message.lower() for entry in error.errors)


@pytest.mark.parametrize("content", [_csv([_execution()]) + b'"unterminated', _csv([_execution()]).rstrip(b"\r\n") + b",extra\r\n"])
def test_parse_raw_malformed_csv_rejects_entire_document(content: bytes) -> None:
    error = _invalid(content)
    assert any(entry.field == "record" for entry in error.errors)


def test_parse_raw_duplicate_csv_header_reports_header_instead_of_silently_overwriting() -> None:
    error = _invalid(_csv([_execution()], (*_V1_FIELDS, "price")))
    assert _locations(error, "price") == {1}


@pytest.mark.parametrize("content", [b'{"schema_version":', b'{"schema_version":"1","executions":[}', b'{"schema_version":"1","executions":[],}'])
def test_parse_raw_malformed_json_reports_safe_document_error(content: bytes) -> None:
    error = _invalid(content)
    assert _locations(error, "document") == {"document"}
    assert "invalid JSON" in error.errors[0].message


@pytest.mark.parametrize("version", ["1", "1.0", "1.42", "1.beta", "1."])
def test_parse_raw_json_supported_major_version_accepts_document(version: str) -> None:
    records, _ = GenericParser().parse_raw(_content([_execution()], "json", schema_version=version), "fills.json")
    assert len(records) == 1


@pytest.mark.parametrize("version", ["2.0", "10", "0.9", "", None, 1, True])
def test_parse_raw_json_unsupported_or_non_string_version_reports_supported_major_version(version: object) -> None:
    error = _invalid(_content([_execution()], "json", schema_version=version))
    assert _locations(error, "schema_version") == {"document"}
    assert any("'1'" in entry.message for entry in error.errors if entry.field == "schema_version")


@pytest.mark.parametrize(
    ("document", "field", "location"),
    [
        ({"executions": [_execution()]}, "schema_version", "document"),
        ({"schema_version": "1"}, "executions", "document"),
        ({"schema_version": "1", "executions": {}}, "executions", "document"),
        ({"schema_version": "1", "executions": ["not-an-object"]}, "record", "executions[0]"),
        ({"schema_version": "1", "executions": [None]}, "record", "executions[0]"),
        ({"schema_version": "1", "executions": [[]]}, "record", "executions[0]"),
        ({"schema_version": "1", "executions": [_execution()], "corporate_actions": {}}, "corporate_actions", "document"),
    ],
)
def test_parse_raw_invalid_json_document_shape_reports_field_without_input_context(document: dict[str, object], field: str, location: str) -> None:
    error = _invalid(json.dumps(document).encode())
    assert location in _locations(error, field)


@pytest.mark.parametrize("as_strings", [False, True])
def test_parse_raw_json_numeric_literals_and_strings_preserve_exact_decimal_values(as_strings: bool) -> None:
    content = _content([_execution(quantity="1234567890", price="0.1", commission="0.00000001", realized_pnl="-0.12345678")], "json")
    if not as_strings:
        for value in ("1234567890", "0.1", "0.00000001", "-0.12345678"):
            content = content.replace(f'"{value}"'.encode(), value.encode())
    records, _ = GenericParser().parse_raw(content, "fills.json")
    raw = records[0]
    assert raw.quantity == Decimal("1234567890")
    assert raw.price == Decimal("0.1")
    assert raw.commission == Decimal("0.00000001")
    assert raw.pnl == Decimal("-0.12345678")
    assert all(isinstance(value, Decimal) for value in (raw.quantity, raw.price, raw.commission, raw.pnl))
    assert _source_record(raw)["price"] == "0.1"


def test_parse_raw_json_corporate_action_maps_split_to_native_pipeline_dto() -> None:
    action = {"symbol": "AAPL", "date": "2026-03-13", "ratio": 4, "old_instrument_id": "before", "new_instrument_id": "after"}
    records, actions = GenericParser().parse_raw(_content([_execution()], "json", corporate_actions=[action]), "fills.json")
    assert len(records) == 1
    assert len(actions) == 1
    assert actions[0].symbol == "AAPL"
    assert actions[0].date == date(2026, 3, 13)
    assert actions[0].ratio == Decimal("4")
    assert isinstance(actions[0].ratio, Decimal)
    assert actions[0].old_conid == "before"
    assert actions[0].new_conid == "after"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", ""),
        ("date", "03/13/2026"),
        ("date", "2026-02-30"),
        ("date", "2026-03-13T00:00:00Z"),
        ("ratio", "0"),
        ("ratio", "-4"),
        ("ratio", "NaN"),
        ("ratio", True),
    ],
)
def test_parse_raw_json_invalid_corporate_action_rejects_valid_executions_too(field: str, value: object) -> None:
    action = {"symbol": "AAPL", "date": "2026-03-13", "ratio": "4", field: value}
    error = _invalid(_content([_execution()], "json", corporate_actions=[action]))
    assert _locations(error, field) == {"corporate_actions[0]"}


def test_parse_raw_json_corporate_action_missing_fields_reports_all_fields() -> None:
    error = _invalid(_content([_execution()], "json", corporate_actions=[{}]))
    assert {entry.field for entry in error.errors} == {"symbol", "date", "ratio"}
    assert {entry.location for entry in error.errors} == {"corporate_actions[0]"}


@pytest.mark.parametrize(("encoding", "filename"), [("json", "fills.csv"), ("csv", "fills.json"), ("json", "fills.xml"), ("csv", "no-extension")])
def test_parse_raw_encoding_detection_ignores_filename(encoding: str, filename: str) -> None:
    prefix = b"\xef\xbb\xbf \r\n" if encoding == "json" else b"\xef\xbb\xbf"
    records, _ = GenericParser().parse_raw(prefix + _content([_execution()], encoding), filename)
    assert len(records) == 1


@pytest.mark.parametrize("filename", ["fills.csv", "fills.json", "fills.xml", "no-extension"])
@pytest.mark.parametrize("content", [b'<?xml version="1.0"?><trades/>', b"\xef\xbb\xbf \r\n<trades/>"])
def test_parse_raw_xml_content_any_filename_rejects_with_supported_encodings(filename: str, content: bytes) -> None:
    with pytest.raises(ImportParseError, match="CSV and JSON only; XML is not supported"):
        GenericParser().parse_raw(content, filename)


def test_parse_raw_non_utf8_bytes_reports_encoding_without_echoing_content() -> None:
    error = _invalid(b"\xff\xfe\x80sensitive-execution")
    assert _locations(error, "encoding") == {"document"}
    assert "sensitive-execution" not in str(error.errors)


def test_parse_raw_hundred_csv_rows_with_two_invalid_rows_reports_physical_lines_12_and_57() -> None:
    rows = [_execution(execution_id=f"fill-{index}") for index in range(99)]
    rows[10]["quantity"] = "0"
    rows[55]["executed_at"] = "2026-03-14T10:31:07"
    error = _invalid(_csv(rows))
    assert {(entry.location, entry.field) for entry in error.errors} == {(12, "quantity"), (57, "executed_at")}


def test_parse_raw_csv_after_quoted_multiline_cell_reports_physical_line_number() -> None:
    rows = [_execution(order_id="two\nlines", instrument_id="equity-contract", open_close="OPEN"), _execution(execution_id="fill-2", quantity="0")]
    error = _invalid(_csv(rows))
    assert _locations(error, "quantity") == {4}


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_invalid_records_and_logs_never_include_sensitive_values_or_pydantic_context(encoding: str, caplog: pytest.LogCaptureFixture) -> None:
    sentinels = ["PRIVATE_EXECUTION_819273", "PRIVATE_SYMBOL_564738", "1234567.87654321", "7654321.12345678", "-8765432.23456789", "PRIVATE_ENUM_746382"]
    record = _execution(execution_id=sentinels[0], symbol=sentinels[1], price=sentinels[2], quantity=sentinels[3], realized_pnl=sentinels[4], side=sentinels[5])
    with caplog.at_level(logging.DEBUG):
        error = _invalid(_content([record], encoding))
    serialized = json.dumps([asdict(entry) for entry in error.errors])
    logs = caplog.text + json.dumps([entry.__dict__ for entry in caplog.records], default=str)
    assert {entry.field for entry in error.errors} == {"side"}
    assert "input" not in serialized
    assert "ctx" not in serialized
    for sentinel in sentinels:
        assert sentinel not in str(error)
        assert sentinel not in serialized
        assert sentinel not in logs


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_invalid_decimal_date_and_enum_messages_only_contain_field_names(encoding: str, caplog: pytest.LogCaptureFixture) -> None:
    changes = {
        field: f"PRIVATE_{field.upper()}_918273" for field in ("quantity", "price", "commission", "realized_pnl", "executed_at", "expiry", "option_type")
    }
    with caplog.at_level(logging.DEBUG):
        error = _invalid(_content([_execution(**changes)], encoding))
    assert {entry.field for entry in error.errors} == set(changes)
    diagnostics = str(error) + str(error.errors) + caplog.text + str([entry.__dict__ for entry in caplog.records])
    for value in changes.values():
        assert value not in diagnostics


def test_get_parser_generic_type_returns_raw_only_generic_parser() -> None:
    parser = get_parser(BrokerType.GENERIC)
    assert isinstance(parser, GenericParser)
    with pytest.raises(NotImplementedError, match="parse_raw"):
        parser.parse(_csv([_execution()]), "fills.csv")


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("asset_class", ["OPTION", "FUTURE"])
@pytest.mark.parametrize("identifier", [None, "", "  "])
def test_parse_raw_plain_derivative_without_instrument_id_rejects_ambiguous_contract(encoding: str, asset_class: str, identifier: object) -> None:
    row = _option(asset_class=asset_class, instrument_id=identifier)
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {"instrument_id"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize(("field", "value"), [("strike", "155"), ("expiry", "2026-07-17"), ("option_type", "P")])
def test_parse_raw_occ_contract_explicit_disagreement_reports_conflicting_field(encoding: str, field: str, value: str) -> None:
    row = _execution(asset_class="OPTION", symbol="AAPL  260619C00150000", multiplier="100", **{field: value})
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {field}
    assert all("OCC" in entry.message for entry in error.errors)


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_occ_contract_all_matching_explicit_fields_accepts_without_identifier(encoding: str) -> None:
    row = _option(symbol="AAPL  260619C00150000", instrument_id=None)
    records, _ = GenericParser().parse_raw(_content([row], encoding), "fills.csv")
    assert records[0].conid is None
    assert records[0].strike == Decimal("150")
    assert records[0].expiry == date(2026, 6, 19)
    assert records[0].option_type == "C"


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("symbol", ["AAPL", "AAPL  260619C00150000"])
def test_parse_raw_single_order_leg_without_instrument_id_rejects_even_valid_occ_symbol(encoding: str, symbol: str) -> None:
    row = _option(symbol=symbol, order_id="public-order", instrument_id=None)
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {"instrument_id"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("marker", [None, "", "  "])
def test_parse_raw_single_order_leg_without_position_effect_reports_marker_field(encoding: str, marker: object) -> None:
    row = _option(order_id="public-order", open_close=marker)
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {"open_close"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_parse_raw_equity_order_missing_both_identifier_and_marker_collects_both_errors(encoding: str) -> None:
    error = _invalid(_content([_execution(order_id="public-order")], encoding))
    assert {entry.field for entry in error.errors} == {"instrument_id", "open_close"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("pnl", [None, "", "  "])
def test_parse_raw_order_linked_close_without_reported_pnl_reports_required_pnl(encoding: str, pnl: object) -> None:
    row = _option(order_id="public-order", open_close="CLOSE", realized_pnl=pnl)
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {"realized_pnl"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("pnl", ["0", "-12.5", "100"])
def test_parse_raw_order_linked_close_preserves_explicit_zero_or_nonzero_pnl(encoding: str, pnl: str) -> None:
    row = _option(order_id="public-order", open_close="CLOSE", realized_pnl=pnl)
    records, _ = GenericParser().parse_raw(_content([row], encoding), "fills.csv")
    assert records[0].pnl == Decimal(pnl)
    assert records[0].open_close == "C"


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("order_id", [None, "public-order"])
@pytest.mark.parametrize("pnl", ["0", "10", "-10"])
def test_parse_raw_explicit_open_with_any_reported_pnl_rejects_contradictory_closing_signal(encoding: str, order_id: str | None, pnl: str) -> None:
    row = _option(order_id=order_id, open_close="OPEN", realized_pnl=pnl)
    error = _invalid(_content([row], encoding))
    assert {entry.field for entry in error.errors} == {"realized_pnl"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("otherwise_invalid", [False, True])
def test_parse_raw_shared_order_mixed_markers_reports_both_rows_even_with_other_invalid_fields(encoding: str, otherwise_invalid: bool) -> None:
    first = _option(order_id="public-order")
    second = {**first, "execution_id": "fill-2", "open_close": "cLoSe", "realized_pnl": "0", "quantity": "0" if otherwise_invalid else "1"}
    error = _invalid(_content([first, second], encoding))
    assert _locations(error, "open_close") == ({2, 3} if encoding == "csv" else {"executions[0]", "executions[1]"})
    if otherwise_invalid:
        assert _locations(error, "quantity") == ({3} if encoding == "csv" else {"executions[1]"})


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("order_id", [None, "", "public-order"])
@pytest.mark.parametrize("cash", ["999", "not-a-decimal", "NaN", {"untrusted": "cash"}, ["untrusted-cash"]])
def test_parse_raw_reserved_unknown_fields_remain_nested_and_cannot_override_trusted_metadata(encoding: str, order_id: str | None, cash: object) -> None:
    original = _option(order_id=order_id, price="3", quantity="1", commission="0")
    extras = {"brokerageOrderID": "hidden-order", "netCash": cash, "openCloseIndicator": "C;O", "source_record": {"netCash": "nested-poison"}}
    parser = GenericParser()
    baseline, _ = parser.parse_raw(_content([original], encoding), "baseline.csv")
    records, _ = parser.parse_raw(_content([{**original, **extras}], encoding), "poisoned.csv")
    assert records[0].model_dump(exclude={"raw_data"}) == baseline[0].model_dump(exclude={"raw_data"})
    assert {key: value for key, value in records[0].raw_data.items() if key != "source_record"} == {
        key: value for key, value in baseline[0].raw_data.items() if key != "source_record"
    }
    source = _source_record(records[0])
    for key, value in extras.items():
        assert source[key] == (str(value) if encoding == "csv" else value)
    assert "openCloseIndicator" not in records[0].raw_data
    if order_id:
        assert records[0].raw_data["brokerageOrderID"] == "public-order"
        assert Decimal(str(records[0].raw_data["netCash"])) == Decimal("-300")
    else:
        assert set(records[0].raw_data) == {"source_record"}
    if encoding == "csv":
        assert len(parser.warnings) == len(extras)


def test_parse_raw_reserved_source_payload_retains_json_decimal_precision_without_binary_float() -> None:
    content = _content([_execution(netCash="0.12345678901234567890123456789", source_record={"price": "0.98765432109876543210987654321"})], "json")
    for value in ("0.12345678901234567890123456789", "0.98765432109876543210987654321"):
        content = content.replace(f'"{value}"'.encode(), value.encode())
    records, _ = GenericParser().parse_raw(content, "fills.json")
    source = _source_record(records[0])
    assert source["netCash"] == "0.12345678901234567890123456789"
    nested = source["source_record"]
    assert isinstance(nested, dict)
    assert nested["price"] == "0.98765432109876543210987654321"
    assert set(records[0].raw_data) == {"source_record"}


@pytest.mark.parametrize("ratio", ["0.000000001", "1e-9", "10000000000", "99999999999.99999999"])
def test_parse_raw_split_ratio_outside_numeric_18_8_rejects_entire_document(ratio: str) -> None:
    actions = [{"symbol": "SPLT", "date": "2026-03-13", "ratio": "4"}, {"symbol": "OTHER", "date": "2026-03-13", "ratio": ratio}]
    error = _invalid(_content([_execution()], "json", corporate_actions=actions))
    assert {(entry.location, entry.field) for entry in error.errors} == {("corporate_actions[1]", "ratio")}
    assert ratio not in str(error.errors)


@pytest.mark.parametrize("ratio", ["0.00000001", "9999999999.99999999"])
def test_parse_raw_split_ratio_numeric_18_8_boundaries_preserves_exact_positive_value(ratio: str) -> None:
    action = {"symbol": "SPLT", "date": "2026-03-13", "ratio": ratio}
    _, actions = GenericParser().parse_raw(_content([_execution()], "json", corporate_actions=[action]), "fills.json")
    assert actions[0].ratio == Decimal(ratio)
    assert actions[0].ratio > 0


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_parse_raw_utc_overflow_collects_timestamp_and_other_row_errors_safely(encoding: str, timestamp: str, caplog: pytest.LogCaptureFixture) -> None:
    rows = [_execution(executed_at=timestamp), _execution(execution_id="fill-2", quantity="0")]
    with caplog.at_level(logging.DEBUG):
        error = _invalid(_content(rows, encoding))
    expected = {(2, "executed_at"), (3, "quantity")} if encoding == "csv" else {("executions[0]", "executed_at"), ("executions[1]", "quantity")}
    assert {(entry.location, entry.field) for entry in error.errors} == expected
    assert any("UTC" in entry.message for entry in error.errors if entry.field == "executed_at")
    assert timestamp not in str(error.errors) + caplog.text


def test_parse_raw_wide_csv_header_preserves_rows_and_warns_once_per_unknown_column() -> None:
    unknown = tuple(f"extra_{index}" for index in range(8192))
    parser = GenericParser()
    records, _ = parser.parse_raw(_csv([_execution(), _execution(execution_id="fill-2")], (*_V1_FIELDS, *unknown)), "wide.csv")
    assert len(records) == 2
    assert len(parser.warnings) == len(unknown)
    assert set(parser.warnings) == {f"Unknown CSV column ignored: {name}." for name in unknown}


def test_parse_raw_wide_csv_header_duplicate_unknown_column_is_reported_once_without_overwriting_silently() -> None:
    unknown = tuple(f"extra_{index}" for index in range(8192))
    parser = GenericParser()
    with pytest.raises(GenericImportValidationError) as caught:
        parser.parse_raw(_csv([_execution()], (*_V1_FIELDS, *unknown, unknown[0])), "wide.csv")
    assert {(entry.location, entry.field) for entry in caught.value.errors} == {(1, "header")}
    assert len(parser.warnings) == len(unknown)


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("asset_class", "eQuItY", True),
        ("asset_class", " fUtUrE ", True),
        ("asset_class", "BOND", False),
        ("side", " bUy ", True),
        ("side", "sell", True),
        ("side", "LONG", False),
        ("open_close", " oPeN ", True),
        ("open_close", "close", True),
        ("open_close", "C;O", False),
        ("option_type", "c", True),
        ("option_type", " P ", True),
        ("option_type", "CALL", False),
    ],
)
def test_generic_input_schema_enum_patterns_and_parser_agree_on_supported_spellings(field: str, value: str, accepted: bool) -> None:
    schema_field = GenericExecutionV1.model_json_schema()["properties"][field]
    alternatives = schema_field.get("anyOf", [schema_field])
    patterns = [alternative["pattern"] for alternative in alternatives if "pattern" in alternative]
    assert patterns
    assert all(pattern.startswith("^") and pattern.endswith("$") and "(?i)" not in pattern for pattern in patterns)
    assert any(re.fullmatch(pattern, value) is not None for pattern in patterns) is accepted
    row = _execution(multiplier="100", instrument_id="stable-contract", **{field: value})
    if accepted:
        records, _ = GenericParser().parse_raw(_content([row], "json"), "fills.json")
        assert len(records) == 1
    else:
        assert _locations(_invalid(_content([row], "json")), field) == {"executions[0]"}


@pytest.mark.parametrize("value", [100, "100", " 100 ", "+100", "00100", "100.0", 0, "0", -1, "-1", 1.5, "1.5", True, False])
def test_generic_input_schema_multiplier_forms_and_parser_agree_on_positive_integers(value: object) -> None:
    schema_field = GenericExecutionV1.model_json_schema()["properties"]["multiplier"]
    branches = schema_field["anyOf"]
    integer_branch = next(branch for branch in branches if branch.get("type") == "integer")
    assert integer_branch["minimum"] == 1
    patterns = [branch["pattern"] for branch in branches if branch.get("type") == "string"]
    advertised = (type(value) is int and value >= 1) or (isinstance(value, str) and any(re.fullmatch(pattern, value) is not None for pattern in patterns))
    accepted = type(value) is int and value > 0 or isinstance(value, str) and value in {"100", " 100 ", "+100", "00100", "100.0"}
    assert advertised is accepted
    row = _option(multiplier=value)
    if accepted:
        records, _ = GenericParser().parse_raw(_content([row], "json"), "fills.json")
        assert records[0].multiplier == 100
    else:
        assert _locations(_invalid(_content([row], "json")), "multiplier") == {"executions[0]"}


def test_generic_input_schema_conditionals_accept_case_variants_and_require_contract_and_order_fields() -> None:
    rules = GenericExecutionV1.model_json_schema()["allOf"]
    asset_rules = [rule for rule in rules if "asset_class" in rule["if"].get("properties", {})]
    for spelling, required in (("oPtIoN", "multiplier"), (" fUtUrE ", "multiplier"), ("future", "instrument_id")):
        assert any(
            required in rule["then"].get("required", []) and re.fullmatch(rule["if"]["properties"]["asset_class"]["pattern"], spelling) for rule in asset_rules
        )
    option_rule = next(rule for rule in asset_rules if "if" in rule["then"])
    assert re.fullmatch(option_rule["if"]["properties"]["asset_class"]["pattern"], " oPtIoN ")
    assert set(option_rule["then"]["then"]["required"]) == {"instrument_id", "expiry", "strike", "option_type"}
    occ_pattern = option_rule["then"]["if"]["not"]["properties"]["symbol"]["pattern"]
    assert re.fullmatch(occ_pattern, "AAPL  260619C00150000")
    assert re.fullmatch(occ_pattern, "AAPL  240229c00150000")
    assert not re.fullmatch(occ_pattern, "AAPL  260229C00150000")
    assert not re.fullmatch(occ_pattern, "AAPL  260619C00000000")
    order_rule = next(rule for rule in rules if rule["if"].get("required") == ["order_id"])
    assert set(order_rule["then"]["required"]) == {"instrument_id", "open_close"}
    order_pattern = order_rule["if"]["properties"]["order_id"]["pattern"]
    assert re.search(order_pattern, "public-order")
    assert not re.search(order_pattern, " \t ")
    close_rule = next(rule for rule in rules if rule["then"].get("required") == ["realized_pnl"])
    close_condition = next(condition for condition in close_rule["if"]["allOf"] if "open_close" in condition.get("properties", {}))
    assert re.fullmatch(close_condition["properties"]["open_close"]["pattern"], " cLoSe ")
    open_rule = next(rule for rule in rules if "not" in rule["then"])
    assert re.fullmatch(open_rule["if"]["properties"]["open_close"]["pattern"], " oPeN ")
    assert open_rule["then"]["not"]["required"] == ["realized_pnl"]


_DECIMAL_FIELDS = (
    ("quantity", "positive"),
    ("price", "nonnegative"),
    ("commission", "nonnegative"),
    ("strike", "positive"),
    ("realized_pnl", "signed"),
    ("ratio", "positive"),
)
_DECIMAL_TEXT_CASES = (
    ("1", "positive"),
    ("+1.25", "positive"),
    (" .5 ", "positive"),
    ("1.", "positive"),
    ("1e2", "positive"),
    ("1e-8", "positive"),
    ("+2.5E-1", "positive"),
    ("\t1.25\r\n", "positive"),
    ("\u00851\u0085", "positive"),
    ("-0", "zero"),
    ("-0.00e+2", "zero"),
    ("0", "zero"),
    ("-1.25", "negative"),
    ("garbage", "invalid"),
    ("1garbage", "invalid"),
    ("prefix1", "invalid"),
    ("NaN", "invalid"),
    ("Infinity", "invalid"),
    ("-Inf", "invalid"),
    ("1,000", "invalid"),
    ("1.2.3", "invalid"),
    ("1e", "invalid"),
    ("1e+", "invalid"),
    ("1_0", "invalid"),
    ("1e2x\n", "invalid"),
    ("\ufeff1", "invalid"),
    ("1\ufeff", "invalid"),
)


def _decimal_content(field: str, value: str) -> bytes:
    if field == "ratio":
        action = {"symbol": "SPLT", "date": "2026-03-13", "ratio": value}
        return _content([_execution()], "json", corporate_actions=[action])
    return _content([_execution(**{field: value})], "json")


@pytest.mark.parametrize(("field", "bound"), _DECIMAL_FIELDS)
@pytest.mark.parametrize(("value", "kind"), _DECIMAL_TEXT_CASES)
def test_generic_input_schema_decimal_text_syntax_and_sign_bounds_match_the_parser(field: str, bound: str, value: str, kind: str) -> None:
    model = GenericCorporateActionV1 if field == "ratio" else GenericExecutionV1
    schema_field = model.model_json_schema(mode="validation")["properties"][field]
    patterns = [branch["pattern"] for branch in schema_field["anyOf"] if branch.get("type") == "string"]
    assert patterns
    assert all(pattern.startswith("^") and pattern.endswith("$") for pattern in patterns)
    accepted = kind != "invalid" and (kind == "positive" or bound == "signed" or kind == "zero" and bound == "nonnegative")
    # JSON Schema pattern uses search semantics. fullmatch would hide an
    # unanchored schema accepting prefixes/suffixes that the importer rejects.
    assert any(re.search(pattern, value) is not None for pattern in patterns) is accepted
    content = _decimal_content(field, value)
    if accepted:
        records, actions = GenericParser().parse_raw(content, "decimal.json")
        parsed = actions[0].ratio if field == "ratio" else getattr(records[0], "pnl" if field == "realized_pnl" else field)
        assert isinstance(parsed, Decimal)
        assert parsed == Decimal(value.strip())
    else:
        error = _invalid(content)
        assert {entry.field for entry in error.errors} == {field}
        expected = "corporate_actions[0]" if field == "ratio" else "executions[0]"
        assert _locations(error, field) == {expected}


@pytest.mark.parametrize(("field", "bound"), _DECIMAL_FIELDS)
@pytest.mark.parametrize("value", ["1e-9", "1e10", "1234567890.123456789"])
@pytest.mark.parametrize("numeric_literal", [False, True])
def test_generic_input_schema_decimal_arithmetic_limits_are_documented_and_always_enforced_by_importer(
    field: str, bound: str, value: str, numeric_literal: bool
) -> None:
    del bound
    model = GenericCorporateActionV1 if field == "ratio" else GenericExecutionV1
    schema_field = model.model_json_schema(mode="validation")["properties"][field]
    patterns = [branch["pattern"] for branch in schema_field["anyOf"] if branch.get("type") == "string"]
    if any(re.search(pattern, value) is not None for pattern in patterns):
        # A future schema may become stricter; do not require it to keep
        # accepting strings whose normalized precision needs arithmetic.
        comment = schema_field.get("$comment", "").lower()
        for requirement in ("numeric(18,8)", "precision", "scale", "exponent", "importer"):
            assert requirement in comment
    content = _decimal_content(field, value)
    if numeric_literal:
        content = content.replace(f'"{value}"'.encode(), value.encode())
    error = _invalid(content)
    assert {entry.field for entry in error.errors} == {field}
    expected = "corporate_actions[0]" if field == "ratio" else "executions[0]"
    assert _locations(error, field) == {expected}
