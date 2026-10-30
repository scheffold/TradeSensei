"""Downloadable reference assets for the generic execution format."""

import csv
import io

from app.schemas.generic_import import GenericExecutionV1, GenericImportDocumentV1

_TEMPLATE_EXECUTIONS: tuple[dict[str, str], ...] = (
    {
        "execution_id": "example-equity-open",
        "executed_at": "2026-03-16T14:30:00Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": "10",
        "price": "150.00",
        "currency": "USD",
        "commission": "1.00",
        "open_close": "OPEN",
    },
    {
        "execution_id": "example-equity-close",
        "executed_at": "2026-03-16T15:30:00Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "SELL",
        "quantity": "10",
        "price": "152.00",
        "currency": "USD",
        "commission": "1.00",
        "open_close": "CLOSE",
    },
    {
        "execution_id": "example-spread-long",
        "executed_at": "2026-03-16T14:35:00Z",
        "symbol": "AAPL",
        "asset_class": "OPTION",
        "side": "BUY",
        "quantity": "1",
        "price": "3.00",
        "currency": "USD",
        "commission": "0.65",
        "open_close": "OPEN",
        "multiplier": "100",
        "instrument_id": "AAPL-20260320-C-150",
        "expiry": "2026-03-20",
        "strike": "150",
        "option_type": "C",
        "order_id": "example-spread-open",
    },
    {
        "execution_id": "example-spread-short",
        "executed_at": "2026-03-16T14:35:00Z",
        "symbol": "AAPL",
        "asset_class": "OPTION",
        "side": "SELL",
        "quantity": "1",
        "price": "1.00",
        "currency": "USD",
        "commission": "0.65",
        "open_close": "OPEN",
        "multiplier": "100",
        "instrument_id": "AAPL-20260320-C-155",
        "expiry": "2026-03-20",
        "strike": "155",
        "option_type": "C",
        "order_id": "example-spread-open",
    },
)


class GenericImportFormatService:
    """Produce static format references without an account or database session."""

    def get_json_schema(self) -> dict[str, object]:
        """Return the JSON Schema generated from the v1 document DTO.

        Returns:
            The document schema and its execution/corporate-action definitions.
        """
        return GenericImportDocumentV1.model_json_schema()

    def get_csv_template(self) -> str:
        """Build a CSV reference containing an equity round trip and option spread.

        Returns:
            CSV text with every v1 field, ready for the normal import pipeline.
        """
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(GenericExecutionV1.model_fields))
        writer.writeheader()
        writer.writerows(_TEMPLATE_EXECUTIONS)
        return output.getvalue()
