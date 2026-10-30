"""Keep generated conditional blank predicates aligned with input normalization."""

import re

import pytest
from pydantic import ValidationError

from app.schemas.generic_import import GenericExecutionV1


def _record(**overrides: object) -> dict[str, object]:
    return {
        "execution_id": "schema-whitespace",
        "executed_at": "2026-03-16T14:30:00Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": "1",
        "price": "1",
        **overrides,
        "currency": "USD",
    }


@pytest.mark.parametrize("whitespace", ["\u0085", "\u001c"])
def test_open_blank_reported_pnl_schema_uses_importer_whitespace(whitespace: str) -> None:
    """Python-only blank characters must not become supplied OPEN P&L in schema."""
    record = GenericExecutionV1.model_validate(_record(open_close="OPEN", realized_pnl=whitespace))
    assert record.realized_pnl is None
    schema = GenericExecutionV1.model_json_schema()
    supplied = schema["allOf"][5]["then"]["not"]
    blank = supplied["properties"]["realized_pnl"]["not"]["anyOf"][1]["pattern"]
    assert re.search(blank, whitespace) is not None
    assert re.search(blank, "0") is None


@pytest.mark.parametrize("whitespace", ["\u0085", "\u001c"])
def test_schema_enum_and_order_blank_predicates_follow_normalization(whitespace: str) -> None:
    """Blank optional orders and stripped enum spellings retain their meaning."""
    record = GenericExecutionV1.model_validate(_record(asset_class=f"{whitespace}equity{whitespace}", order_id=whitespace))
    assert record.instrument_id is None
    assert record.order_id is None
    schema = GenericExecutionV1.model_json_schema()
    enum_pattern = schema["properties"]["asset_class"]["pattern"]
    assert re.search(enum_pattern, f"{whitespace}equity{whitespace}") is not None
    order_present = schema["allOf"][3]["if"]["properties"]["order_id"]["pattern"]
    assert re.search(order_present, whitespace) is None
    assert re.search(order_present, "public-order") is not None


@pytest.mark.parametrize(("whitespace", "accepted"), [("\u0085", True), ("\u001c", False), ("\ufeff", False)])
def test_schema_integer_text_uses_numeric_parser_whitespace(whitespace: str, accepted: bool) -> None:
    """Integer coercion trims Unicode whitespace, but not Python's control extras."""
    value = f"{whitespace}100{whitespace}"
    if accepted:
        assert GenericExecutionV1.model_validate(_record(multiplier=value)).multiplier == 100
    else:
        with pytest.raises(ValidationError):
            GenericExecutionV1.model_validate(_record(multiplier=value))
    variants = GenericExecutionV1.model_json_schema()["properties"]["multiplier"]["anyOf"]
    matches = any(variant.get("type") == "string" and re.search(variant["pattern"], value) is not None for variant in variants)
    assert matches is accepted
