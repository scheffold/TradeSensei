#!/usr/bin/env python3
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

r"""Convert a native broker export to deterministic generic v1 CSV and JSON.

Run from the repository root using the backend environment::

    uv run --project backend python tools/convert_to_generic.py \
        tests/test_data/ibkr/combo_positions.xml --broker ibkr \
        --output-stem tests/test_data/generic/ibkr_combo_positions

Use ``--broker schwab`` for a native Schwab JSON export. The output stem gets
``.csv`` and ``.json`` suffixes. Executions are sorted by UTC timestamp and ID;
decimal values are written as strings without binary floating-point conversion.
Only the native parser's ``parse_raw()`` is used: nothing is aggregated or saved
to a database. Files with corporate actions are rejected because CSV cannot
represent those actions; encode such files directly in the generic JSON format.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path

# Native parser DTOs import the ORM registry, which initializes settings. These
# offline-only defaults allow conversion without a configured server or database.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("SECRET_KEY", "offline-generic-converter-not-an-authentication-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")

from app.schemas.broker_raw_transaction import BrokerRawTransactionCreate  # noqa: E402
from app.schemas.generic_import import GenericExecutionV1, GenericImportDocumentV1  # noqa: E402
from app.utils.parsers.base import BrokerParser  # noqa: E402
from app.utils.parsers.ibkr_xml import IBKRXMLParser  # noqa: E402
from app.utils.parsers.schwab_json import SchwabJSONParser  # noqa: E402


def _execution_record(raw: BrokerRawTransactionCreate) -> GenericExecutionV1:
    """Map a native raw execution to the public v1 fields without aggregation."""
    order_id = raw.raw_data.get("brokerageOrderID")
    return GenericExecutionV1.model_validate(
        {
            "execution_id": raw.id.removeprefix(f"{raw.broker_type}:"),
            "executed_at": raw.executed_at,
            "symbol": raw.symbol,
            "asset_class": raw.asset_class,
            "side": "BUY" if raw.direction == "LONG" else "SELL",
            "quantity": raw.quantity,
            "price": raw.price,
            "currency": raw.currency,
            "commission": raw.commission,
            "open_close": {"O": "OPEN", "C": "CLOSE"}.get(raw.open_close or ""),
            "multiplier": raw.multiplier,
            "instrument_id": raw.conid,
            "expiry": raw.expiry,
            "strike": raw.strike,
            "option_type": raw.option_type,
            "order_id": str(order_id) if order_id else None,
            "realized_pnl": raw.pnl,
        }
    )


def convert_fixture(source: Path, broker: str, output_stem: Path) -> tuple[Path, Path]:
    """Write CSV and JSON conversions and return their paths.

    Args:
        source: Native IBKR XML or wrapped Schwab JSON export.
        broker: ``ibkr`` or ``schwab``.
        output_stem: Destination path without an encoding suffix.

    Raises:
        ValueError: If the broker is unsupported or the file contains corporate
            actions that cannot be represented in both encodings.
    """
    parsers: dict[str, type[BrokerParser]] = {"ibkr": IBKRXMLParser, "schwab": SchwabJSONParser}
    if broker not in parsers:
        raise ValueError("broker must be ibkr or schwab")
    rows, corporate_actions = parsers[broker]().parse_raw(source.read_bytes(), source.name)
    if corporate_actions:
        raise ValueError("Corporate actions require generic JSON; this two-format converter cannot encode them in CSV.")
    executions = [_execution_record(raw) for raw in sorted(rows, key=lambda raw: (raw.executed_at, raw.id))]
    document = GenericImportDocumentV1(schema_version="1", executions=executions)

    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=list(GenericExecutionV1.model_fields), lineterminator="\n")
    writer.writeheader()
    for execution in executions:
        writer.writerow(execution.model_dump(mode="json"))

    csv_path = output_stem.with_suffix(".csv")
    json_path = output_stem.with_suffix(".json")
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(csv_buffer.getvalue(), encoding="utf-8", newline="")
    json_path.write_text(json.dumps(document.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return csv_path, json_path


def main() -> None:
    """Parse command-line options and convert one native export."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Native IBKR XML or wrapped Schwab JSON file")
    parser.add_argument("--broker", required=True, choices=("ibkr", "schwab"))
    parser.add_argument("--output-stem", required=True, type=Path, help="Destination path without .csv/.json")
    args = parser.parse_args()
    for path in convert_fixture(args.source, args.broker, args.output_stem):
        print(path)


if __name__ == "__main__":
    main()
