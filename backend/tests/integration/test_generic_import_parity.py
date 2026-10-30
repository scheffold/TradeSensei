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

"""Native versus generic trade, leg-grouping, and order-cash parity."""

from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.broker_raw_transaction import BrokerRawTransaction
from app.models.trade import Trade
from app.services.import_service import ImportService
from app.utils.parsers.factory import BrokerType

_DATA = Path(__file__).resolve().parents[3] / "tests" / "test_data"
_CASES = [
    ("ibkr", "TraderJournal_anonymized_combined.xml", "ibkr_combined"),
    ("ibkr", "combo_positions.xml", "ibkr_combo_positions"),
    ("schwab", "schwab_aggregation.json", "schwab_aggregation"),
    ("schwab", "schwab_short_roundtrip.json", "schwab_short_roundtrip"),
]
_CENT = Decimal("0.01")


async def _trade_snapshot(session: AsyncSession, account_id: uuid.UUID) -> Counter[tuple[object, ...]]:
    """Compare business fields and exact leg membership, never generated IDs."""
    trades = (await session.scalars(select(Trade).where(Trade.account_id == account_id).options(selectinload(Trade.logical_executions)))).all()
    snapshot: Counter[tuple[object, ...]] = Counter()
    for trade in trades:
        legs = tuple(
            sorted(
                (
                    (
                        leg.symbol,
                        leg.conid,
                        leg.asset_class,
                        leg.direction,
                        leg.quantity,
                        leg.price,
                        leg.commission,
                        leg.executed_at,
                        leg.pnl,
                        leg.multiplier,
                        leg.open_close,
                        leg.strike,
                        leg.expiry,
                        leg.option_type,
                    )
                    for leg in trade.logical_executions
                ),
                key=repr,
            )
        )
        snapshot[
            (
                trade.symbol,
                trade.asset_class,
                trade.direction,
                trade.quantity,
                trade.entry_price,
                trade.exit_price,
                trade.commission,
                trade.opened_at,
                trade.closed_at,
                trade.pnl,
                trade.net_pnl,
                trade.currency,
                trade.multiplier,
                trade.is_combo,
                legs,
            )
        ] += 1
    return snapshot


async def _cash_snapshot(session: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID, broker: str) -> dict[str, tuple[str, Decimal]]:
    """Read order metadata in its pipeline representation, rounding cash only."""
    rows = (await session.scalars(select(BrokerRawTransaction).where(BrokerRawTransaction.account_id == account_id))).all()
    prefix = f"{user_id}:generic:{account_id}:" if broker == "generic" else f"{user_id}:{broker}:"
    snapshot: dict[str, tuple[str, Decimal]] = {}
    for raw in rows:
        order_id = raw.raw_data.get("brokerageOrderID")
        if order_id:
            snapshot[raw.id.removeprefix(prefix)] = (str(order_id), Decimal(str(raw.raw_data["netCash"])).quantize(_CENT))
    return snapshot


@pytest.mark.integration
@pytest.mark.parametrize(("broker", "source_name", "generic_stem"), _CASES, ids=[case[2] for case in _CASES])
async def test_native_csv_json_trade_and_combo_parity(
    client: AsyncClient,
    db_session: AsyncSession,
    primary_user: dict[str, object],
    broker: str,
    source_name: str,
    generic_stem: str,
) -> None:
    """All three encodings must yield the same positions and their exact legs."""
    # primary_user is the shared authenticated ownership fixture from conftest.
    headers = cast(dict[str, str], primary_user["headers"])
    user_id = uuid.UUID(str(primary_user["user_id"]))
    portfolio = await client.post("/api/v1/portfolios", headers=headers, json={"name": "Generic parity", "base_currency": "USD"})
    assert portfolio.status_code == 201, portfolio.text
    portfolio_id = uuid.UUID(portfolio.json()["data"]["id"])
    trade_snapshots = []
    cash_snapshots = []
    service = ImportService(db_session)
    sources = [
        (broker, _DATA / broker / source_name),
        ("generic", _DATA / "generic" / f"{generic_stem}.csv"),
        ("generic", _DATA / "generic" / f"{generic_stem}.json"),
    ]
    for account_type, source in sources:
        response = await client.post(
            f"/api/v1/portfolios/{portfolio_id}/accounts",
            headers=headers,
            json={"name": source.name, "type": account_type, "account_number": "45302130" if account_type == "schwab" else None},
        )
        assert response.status_code == 201, response.text
        account_id = uuid.UUID(response.json()["data"]["id"])
        result = await service.import_trades(
            content=source.read_bytes(),
            filename=source.name,
            broker=BrokerType(account_type),
            portfolio_id=portfolio_id,
            account_id=account_id,
            user_id=user_id,
        )
        assert result.raw_transactions_created > 0
        trade_snapshots.append(await _trade_snapshot(db_session, account_id))
        cash_snapshots.append(await _cash_snapshot(db_session, account_id, user_id, account_type))

    assert trade_snapshots[0]
    assert trade_snapshots[0] == trade_snapshots[1] == trade_snapshots[2]
    assert cash_snapshots[1] == cash_snapshots[2]
    assert cash_snapshots[0].keys() == cash_snapshots[1].keys()
    differences = {
        execution_id: (native, cash_snapshots[1][execution_id])
        for execution_id, native in cash_snapshots[0].items()
        if native != cash_snapshots[1][execution_id]
    }
    # One BUY-close cash override in the native fixture contradicts its own
    # quantity/price/fees. v1 deliberately derives cash, not broker overrides.
    # See design.md and docs/generic-import-format.md: this must not hide any
    # trade/P&L/grouping mismatch or any additional raw-cash discrepancy.
    expected = (
        {"900000011": (("combo-meta-close-2", Decimal("3153.00")), ("combo-meta-close-2", Decimal("-6303.69")))}
        if generic_stem == "ibkr_combo_positions"
        else {}
    )
    assert differences == expected
