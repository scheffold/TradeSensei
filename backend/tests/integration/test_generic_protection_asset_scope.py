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

"""An unrelated null-ID orphan close must not inherit equity protection (F7)."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker_raw_transaction import BrokerRawTransaction
from app.models.logical_execution import LogicalExecution
from app.models.trade import Trade

pytestmark = pytest.mark.integration

_OPENED_AT = datetime(2026, 3, 14, 14, 30, tzinfo=UTC)
_POSITION_FIELDS = (
    "symbol",
    "asset_class",
    "direction",
    "quantity",
    "entry_price",
    "exit_price",
    "opened_at",
    "closed_at",
    "pnl",
    "net_pnl",
    "commission",
    "currency",
    "is_combo",
)
_PROTECTION_FIELDS = (
    "journal",
    "user_rating",
    "stop_loss",
    "take_profit",
    "is_manually_edited",
    "has_conflict",
    "has_unlinked_executions",
)


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    portfolio_id: str,
    account_id: str,
    rows: list[dict[str, str]],
    encoding: str,
) -> dict[str, Any]:
    if encoding == "json":
        content = json.dumps({"schema_version": "1.0", "executions": rows}).encode()
        media_type = "application/json"
    else:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        content = stream.getvalue().encode()
        media_type = "text/csv"
    response = await client.post(
        f"/api/v1/portfolios/{portfolio_id}/imports/upload",
        headers=headers,
        data={"account_id": account_id},
        files={"file": (f"asset-scope.{encoding}", content, media_type)},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["data"]
    assert result["broker"] == "generic"
    return result


async def _account_trades(client: AsyncClient, headers: dict[str, str], portfolio_id: str, account_id: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/portfolios/{portfolio_id}/trades",
        headers=headers,
        params={"account_id": account_id, "limit": 10},
    )
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["next_cursor"] is None
    items: list[dict[str, Any]] = page["items"]
    assert all(trade["account_id"] == account_id for trade in items)
    return items


async def _lineage(db_session: AsyncSession, user_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
    result = await db_session.execute(
        select(
            LogicalExecution.__table__,
            BrokerRawTransaction.id.label("raw_id"),
            BrokerRawTransaction.conid.label("raw_conid"),
            BrokerRawTransaction.pnl.label("raw_pnl"),
            BrokerRawTransaction.raw_data,
        )
        .select_from(LogicalExecution)
        .join(BrokerRawTransaction, BrokerRawTransaction.id == LogicalExecution.source_id)
        .where(
            LogicalExecution.user_id == user_id,
            LogicalExecution.account_id == account_id,
            BrokerRawTransaction.user_id == user_id,
            BrokerRawTransaction.account_id == account_id,
        )
        .order_by(LogicalExecution.source_id)
    )
    return [dict(row) for row in result.mappings()]


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize(
    ("asset_class", "symbol"),
    [("CRYPTO", "AAPL"), ("FOREX", "AAPL"), ("EQUITY", "MSFT")],
    ids=["same-symbol-crypto", "same-symbol-forex", "different-symbol-equity"],
)
async def test_upload_generic_unrelated_orphan_close_with_protected_equity_preserves_links_and_metadata(
    client: AsyncClient,
    db_session: AsyncSession,
    primary_user: dict[str, Any],
    encoding: str,
    asset_class: str,
    symbol: str,
) -> None:
    headers = primary_user["headers"]
    user_id = UUID(primary_user["user_id"])
    portfolio = await client.post(
        "/api/v1/portfolios",
        headers=headers,
        json={"name": "F7 protection scope", "base_currency": "USD"},
    )
    assert portfolio.status_code == 201, portfolio.text
    portfolio_id = portfolio.json()["data"]["id"]

    opening = {
        "execution_id": "equity-open",
        "executed_at": _OPENED_AT.isoformat(),
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": "10",
        "price": "100",
        "currency": "USD",
        "commission": "1",
        "open_close": "OPEN",
    }
    closing = {
        **opening,
        "execution_id": "equity-close",
        "side": "SELL",
        "price": "105",
        "executed_at": (_OPENED_AT + timedelta(hours=1)).isoformat(),
        "open_close": "CLOSE",
    }
    # No order/instrument ID or reported P&L: ordinary CLOSE uses the zero marker.
    orphan = {
        **opening,
        "execution_id": "independent-close",
        "symbol": symbol,
        "asset_class": asset_class,
        "side": "SELL",
        "quantity": "2",
        "price": "25",
        "commission": "0.25",
        "open_close": "CLOSE",
    }
    baseline_projection: dict[str, Any] | None = None

    for protected in (False, True):
        account = await client.post(
            f"/api/v1/portfolios/{portfolio_id}/accounts",
            headers=headers,
            json={"name": "Protected" if protected else "Baseline", "type": "generic"},
        )
        assert account.status_code == 201, account.text
        account_id = account.json()["data"]["id"]
        first = await _upload(client, headers, portfolio_id, account_id, [opening, closing], encoding)
        assert first["raw_transactions_created"] == 2
        assert first["trades_created"] == 1
        initial_trades = await _account_trades(client, headers, portfolio_id, account_id)
        assert len(initial_trades) == 1
        equity_before = initial_trades[0]
        equity_id = equity_before["id"]
        assert datetime.fromisoformat(equity_before["opened_at"]) == _OPENED_AT
        assert equity_before["closed_at"] is not None
        if protected:
            response = await client.patch(
                f"/api/v1/portfolios/{portfolio_id}/trades/{equity_id}",
                headers=headers,
                json={"journal": "Keep the equity review", "user_rating": 4, "stop_loss": "95", "take_profit": "110"},
            )
            assert response.status_code == 200, response.text
            equity_before = response.json()["data"]
        assert equity_before["is_manually_edited"] is protected
        assert equity_before["has_unlinked_executions"] is False
        assert equity_before["has_conflict"] is False
        original_lineage = await _lineage(db_session, user_id, UUID(account_id))
        assert len(original_lineage) == 2
        assert all(row["conid"] is None and row["raw_conid"] is None for row in original_lineage)

        imported_lineage: list[dict[str, Any]] | None = None
        for expected_new_raw in (1, 0):
            result = await _upload(client, headers, portfolio_id, account_id, [orphan], encoding)
            assert result["raw_transactions_created"] == expected_new_raw
            assert result["trades_created"] == expected_new_raw, result
            assert result["unlinked_executions"] == 0, result
            assert result["trades_protected_skipped"] == 0, result
            assert result["protected_positions_closed"] == 0, result
            assert not any("protected_" in warning for warning in result["warnings"]), result

            trades = await _account_trades(client, headers, portfolio_id, account_id)
            assert len(trades) == 2, trades
            equity = next(trade for trade in trades if trade["id"] == equity_id)
            for field in ("id", *_POSITION_FIELDS, *_PROTECTION_FIELDS):
                assert equity[field] == equity_before[field], field
            reconstructed = next(trade for trade in trades if trade["id"] != equity_id)
            assert reconstructed["symbol"] == symbol
            assert reconstructed["asset_class"] == asset_class
            assert reconstructed["direction"] == "LONG"
            assert reconstructed["is_combo"] is False
            assert datetime.fromisoformat(reconstructed["opened_at"]) == _OPENED_AT
            assert datetime.fromisoformat(reconstructed["closed_at"]) == _OPENED_AT + timedelta(seconds=1)
            for field, expected in {"quantity": "2", "entry_price": "0.001", "exit_price": "25", "commission": "0.25", "pnl": "0"}.items():
                assert Decimal(reconstructed[field]) == Decimal(expected), field
            assert reconstructed["journal"] is None
            assert reconstructed["is_manually_edited"] is False
            assert reconstructed["has_unlinked_executions"] is False
            assert reconstructed["has_conflict"] is False
            projection = {field: reconstructed[field] for field in _POSITION_FIELDS}
            if baseline_projection is None:
                baseline_projection = projection
            assert projection == baseline_projection

            lineage = await _lineage(db_session, user_id, UUID(account_id))
            assert len(lineage) == 3
            prefix = f"{user_id}:generic:{account_id}:"
            assert {row["raw_id"] for row in lineage} == {prefix + execution_id for execution_id in ("equity-open", "equity-close", "independent-close")}
            assert all(row["source_id"] == row["raw_id"] for row in lineage)
            assert all(row["conid"] is None and row["raw_conid"] is None for row in lineage)
            closing_leg = next(row for row in lineage if row["source_id"] == prefix + "independent-close")
            assert closing_leg["trade_id"] == UUID(reconstructed["id"])
            assert str(closing_leg["asset_class"]) == asset_class
            assert closing_leg["symbol"] == symbol
            assert str(closing_leg["direction"]) == "SHORT"
            assert closing_leg["open_close"] == "C"
            assert closing_leg["pnl"] == closing_leg["raw_pnl"] == Decimal("0")
            assert closing_leg["raw_data"]["source_record"] == orphan
            assert closing_leg["origin"] == "imported"
            assert closing_leg["deleted"] is False
            assert closing_leg["user_modified"] is False
            if protected:
                assert [row for row in lineage if row["trade_id"] == UUID(equity_id)] == original_lineage
            for model, count in ((BrokerRawTransaction, 3), (LogicalExecution, 3), (Trade, 2)):
                assert (
                    await db_session.scalar(select(func.count()).select_from(model).where(model.user_id == user_id, model.account_id == UUID(account_id)))
                    == count
                )
            if imported_lineage is not None:
                assert lineage == imported_lineage
            imported_lineage = lineage
