"""Generic uploads and format downloads against the real, isolated test DB."""

import csv
import io
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.constants import GENERIC_IMPORT_MAX_ERRORS
from app.core.exceptions import AccountValidationError, ImportNotAllowedError
from app.models.account import Account, AccountType
from app.models.account_property import AccountProperty
from app.models.broker_raw_transaction import BrokerRawTransaction
from app.models.corporate_action import CorporateAction
from app.models.logical_execution import LogicalExecution
from app.models.portfolio import Portfolio
from app.models.trade import AssetClass, Direction, Trade
from app.models.user import User
from app.repositories.account_repository import AccountRepository
from app.repositories.trade_repository import TradeRepository
from app.schemas.account import AccountUpdate
from app.schemas.generic_import import GenericExecutionV1, GenericImportDocumentV1
from app.services.account_service import AccountService
from app.services.import_service import ImportService
from app.utils.parsers.factory import BrokerType

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DOWNLOAD_PREFIX = "/api/v1/imports/generic"
_REQUIRED_FIELDS = {"execution_id", "executed_at", "symbol", "asset_class", "side", "quantity", "price", "currency"}


@dataclass(frozen=True)
class _ImportContext:
    headers: dict[str, str]
    user_id: UUID
    portfolio_id: UUID
    account_id: UUID

    @property
    def upload_url(self) -> str:
        return f"/api/v1/portfolios/{self.portfolio_id}/imports/upload"

    @property
    def accounts_url(self) -> str:
        return f"/api/v1/portfolios/{self.portfolio_id}/accounts"


async def _create_portfolio(client: AsyncClient, headers: dict[str, str], base_currency: str = "USD") -> UUID:
    response = await client.post(
        "/api/v1/portfolios",
        headers=headers,
        json={"name": f"Generic import {uuid4().hex[:8]}", "base_currency": base_currency},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["id"])


async def _create_account(client: AsyncClient, headers: dict[str, str], portfolio_id: UUID, account_type: str = "generic") -> UUID:
    response = await client.post(
        f"/api/v1/portfolios/{portfolio_id}/accounts",
        headers=headers,
        json={"name": f"Other broker {uuid4().hex[:8]}", "type": account_type},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["id"])


async def _create_context(client: AsyncClient, user: dict[str, Any], account_type: str = "generic") -> _ImportContext:
    headers = dict(user["headers"])
    portfolio_id = await _create_portfolio(client, headers)
    account_id = await _create_account(client, headers, portfolio_id, account_type)
    return _ImportContext(headers, UUID(user["user_id"]), portfolio_id, account_id)


def _execution(**changes: object) -> dict[str, object]:
    return {
        "execution_id": "fill-open",
        "executed_at": "2026-03-14T10:31:07Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": "2",
        "price": "100",
        "currency": "USD",
        **changes,
    }


def _round_trip() -> list[dict[str, object]]:
    return [
        _execution(commission="1", open_close="OPEN"),
        _execution(
            execution_id="fill-close", executed_at="2026-03-14T11:31:07Z", side="SELL", price="110", commission="1", open_close="CLOSE", realized_pnl="20"
        ),
    ]


def _option(**changes: object) -> dict[str, object]:
    return _execution(
        **{
            "asset_class": "OPTION",
            "quantity": "1",
            "price": "3",
            "multiplier": "100",
            "expiry": "2026-06-19",
            "strike": "150",
            "option_type": "C",
            "instrument_id": "call-150",
            "open_close": "OPEN",
            **changes,
        }
    )


def _spread(*, closing_pnl: str | None = None) -> list[dict[str, object]]:
    rows = [
        _option(execution_id="long-open", order_id="spread-open"),
        _option(execution_id="short-open", side="SELL", price="1", strike="155", instrument_id="call-155", order_id="spread-open"),
        _option(execution_id="long-close", side="SELL", price="4", open_close="CLOSE", order_id="spread-close", executed_at="2026-03-14T11:31:07Z"),
        _option(
            execution_id="short-close",
            side="BUY",
            price="1.5",
            strike="155",
            instrument_id="call-155",
            open_close="CLOSE",
            order_id="spread-close",
            executed_at="2026-03-14T11:31:07Z",
        ),
    ]
    if closing_pnl is not None:
        rows[2]["realized_pnl"] = "100" if closing_pnl == "profit" else "0"
        rows[3]["realized_pnl"] = "-50" if closing_pnl == "profit" else "0"
        if closing_pnl == "zero":
            rows[2]["price"], rows[3]["price"] = "3", "1"
    return rows


def _content(rows: Sequence[dict[str, object]], encoding: str = "csv", **document_fields: object) -> bytes:
    if encoding == "json":
        return json.dumps({"schema_version": "1.0", "executions": rows, **document_fields}, default=str).encode("utf-8")
    output = io.StringIO(newline="")
    fields = list(GenericExecutionV1.model_fields)
    for row in rows:
        fields.extend(field for field in row if field not in fields)
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


async def _upload(client: AsyncClient, context: _ImportContext, content: bytes, filename: str = "fills.csv") -> Response:
    return await client.post(
        context.upload_url,
        headers=context.headers,
        data={"account_id": str(context.account_id)},
        files={"file": (filename, content, "application/octet-stream")},
    )


async def _pipeline_ids(session: AsyncSession, account_id: UUID) -> dict[str, set[str]]:
    raw_ids = (await session.scalars(select(BrokerRawTransaction.id).where(BrokerRawTransaction.account_id == account_id))).all()
    execution_ids = (await session.scalars(select(LogicalExecution.id).where(LogicalExecution.account_id == account_id))).all()
    trade_ids = (await session.scalars(select(Trade.id).where(Trade.account_id == account_id))).all()
    return {"raw": set(raw_ids), "executions": {str(value) for value in execution_ids}, "trades": {str(value) for value in trade_ids}}


async def _assert_no_pipeline_rows(session: AsyncSession, account_id: UUID) -> None:
    assert await _pipeline_ids(session, account_id) == {"raw": set(), "executions": set(), "trades": set()}


@asynccontextmanager
async def _committed_test_account(
    engine: object, account_type: AccountType = AccountType.GENERIC
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], _ImportContext]]:
    """Use independent transactions on the configured test engine; clean our user."""
    assert isinstance(engine, AsyncEngine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = _ImportContext({}, uuid4(), uuid4(), uuid4())
    async with factory() as setup:
        setup.add(User(id=context.user_id, username=f"generic_lock_{context.user_id.hex}", email=f"{context.user_id.hex}@example.test"))
        await setup.flush()
        setup.add(Portfolio(id=context.portfolio_id, user_id=context.user_id, name="Generic concurrency test", base_currency="USD"))
        await setup.flush()
        setup.add(
            Account(id=context.account_id, user_id=context.user_id, portfolio_id=context.portfolio_id, name="Lock test", type=account_type, currency="USD")
        )
        await setup.commit()
    try:
        yield factory, context
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(User).where(User.id == context.user_id))
            await cleanup.commit()
            assert await cleanup.scalar(select(User.id).where(User.id == context.user_id)) is None


def _stored_snapshot(row: Trade | LogicalExecution) -> dict[str, object]:
    return deepcopy({column.key: getattr(row, column.key) for column in row.__mapper__.column_attrs})


def _source_record(record: BrokerRawTransaction) -> dict[str, object]:
    source = record.raw_data["source_record"]
    assert isinstance(source, dict)
    return cast(dict[str, object], source)


async def _projection_signature(session: AsyncSession, account_id: UUID) -> list[tuple[object, ...]]:
    trades = (await session.scalars(select(Trade).where(Trade.account_id == account_id))).all()
    executions = (await session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == account_id))).all()
    signatures: list[tuple[object, ...]] = []
    for trade in trades:
        legs = [
            (leg.symbol, leg.asset_class, leg.conid, leg.direction, leg.quantity, leg.price, leg.open_close, leg.is_mixed_execution, leg.executed_at)
            for leg in executions
            if leg.trade_id == trade.id
        ]
        signatures.append(
            (
                trade.symbol,
                trade.asset_class,
                trade.direction,
                trade.quantity,
                trade.entry_price,
                trade.exit_price,
                trade.commission,
                trade.pnl,
                trade.net_pnl,
                trade.opened_at,
                trade.closed_at,
                trade.multiplier,
                trade.is_combo,
                tuple(sorted(legs, key=repr)),
            )
        )
    unlinked = [
        (leg.symbol, leg.asset_class, leg.conid, leg.direction, leg.quantity, leg.price, leg.open_close, leg.is_mixed_execution, leg.executed_at)
        for leg in executions
        if leg.trade_id is None
    ]
    if unlinked:
        signatures.append(("unlinked-executions", tuple(sorted(unlinked, key=repr))))
    return sorted(signatures, key=repr)


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("scope", ["other-account", "different-symbol", "different-asset"])
@pytest.mark.parametrize("closed", [False, True])
async def test_upload_generic_protected_null_id_trade_cannot_capture_independent_business_identity(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, scope: str, closed: bool
) -> None:
    context = await _create_context(client, primary_user)
    protected_rows = _round_trip() if closed else [_round_trip()[0]]
    imported = await _upload(client, context, _content(protected_rows, encoding), f"protected.{encoding}")
    assert imported.status_code == 200, imported.text
    protected = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    protected.is_manually_edited = True
    protected.journal = "Protected trading rationale"
    protected.user_rating = 5
    protected.stop_loss = Decimal("95")
    await db_session.flush()
    await db_session.refresh(protected)
    protected_id = protected.id
    before_trade = _stored_snapshot(protected)
    old_executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    before_executions = {execution.id: _stored_snapshot(execution) for execution in old_executions}
    target = context
    if scope == "other-account":
        account_id = await _create_account(client, context.headers, context.portfolio_id)
        target = _ImportContext(context.headers, context.user_id, context.portfolio_id, account_id)
    row = _execution(
        execution_id="independent",
        symbol="QQQ" if scope == "different-symbol" else "AAPL",
        asset_class="CRYPTO" if scope == "different-asset" else "EQUITY",
        quantity="3",
        price="50",
        open_close="OPEN",
    )
    keys = await TradeRepository(db_session).list_manually_edited_keys(context.user_id, context.portfolio_id, account_id=target.account_id)
    expected_keys = set() if scope == "other-account" else {(context.account_id, "AAPL", AssetClass.EQUITY, None, protected.opened_at)}
    assert keys == expected_keys
    response = await _upload(client, target, _content([row], encoding), f"independent.{encoding}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["raw_transactions_created"] == 1
    assert response.json()["data"]["trades_protected_skipped"] == 0
    independent = (await db_session.scalars(select(Trade).where(Trade.account_id == target.account_id, Trade.id != protected_id))).one()
    assert independent.symbol == row["symbol"]
    assert independent.asset_class == row["asset_class"]
    assert independent.direction == Direction.LONG
    assert independent.quantity == Decimal("3")
    assert independent.entry_price == Decimal("50")
    assert independent.closed_at is None
    assert independent.is_manually_edited is False
    assert independent.broker_conid is None
    source_id = f"{context.user_id}:generic:{target.account_id}:independent"
    linked = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.source_id == source_id))).one()
    assert linked.trade_id == independent.id
    assert linked.trade_id != protected_id
    after_state = await _pipeline_ids(db_session, target.account_id)
    reimport = await _upload(client, target, _content([row], encoding), f"independent.{encoding}")
    assert reimport.status_code == 200, reimport.text
    assert reimport.json()["data"]["raw_transactions_created"] == 0
    assert reimport.json()["data"]["trades_created"] == 0
    assert await _pipeline_ids(db_session, target.account_id) == after_state
    await db_session.refresh(protected)
    assert _stored_snapshot(protected) == before_trade
    for execution in old_executions:
        await db_session.refresh(execution)
        assert _stored_snapshot(execution) == before_executions[execution.id]


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("order_id", [None, "", "public-order"])
async def test_upload_generic_reserved_extras_are_inert_for_actual_trade_grouping_cash_and_flags(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, order_id: str | None
) -> None:
    baseline = await _create_context(client, primary_user)
    rows = [
        _option(execution_id="long-leg", order_id=order_id),
        _option(execution_id="short-leg", side="SELL", price="1", strike="155", instrument_id="call-155", order_id=order_id),
    ]
    response = await _upload(client, baseline, _content(rows, encoding), f"baseline.{encoding}")
    assert response.status_code == 200, response.text
    expected = await _projection_signature(db_session, baseline.account_id)
    assert len(expected) == (1 if order_id else 2)
    for cash in ("999", "not-a-decimal", "NaN", {"untrusted": "cash"}, ["untrusted-cash"]):
        account_id = await _create_account(client, baseline.headers, baseline.portfolio_id)
        poisoned = _ImportContext(baseline.headers, baseline.user_id, baseline.portfolio_id, account_id)
        extras = {"brokerageOrderID": "hidden-order", "netCash": cash, "openCloseIndicator": "C;O", "source_record": {"netCash": "nested-poison"}}
        poisoned_rows = [{**row, **extras} for row in rows]
        response = await _upload(client, poisoned, _content(poisoned_rows, encoding), f"poisoned.{encoding}")
        assert response.status_code == 200, response.text
        assert await _projection_signature(db_session, account_id) == expected
        raw_rows = (await db_session.scalars(select(BrokerRawTransaction).where(BrokerRawTransaction.account_id == account_id))).all()
        assert len(raw_rows) == 2
        for raw in raw_rows:
            assert "openCloseIndicator" not in raw.raw_data
            assert _source_record(raw)["netCash"] == (str(cash) if encoding == "csv" else cash)
            assert _source_record(raw)["source_record"] == (str(extras["source_record"]) if encoding == "csv" else extras["source_record"])
            if order_id:
                assert raw.raw_data["brokerageOrderID"] == "public-order"
                expected_cash = Decimal("-300") if raw.direction == Direction.LONG else Decimal("100")
                assert Decimal(str(raw.raw_data["netCash"])) == expected_cash
            else:
                assert set(raw.raw_data) == {"source_record"}
        executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == account_id))).all()
        assert all(not execution.is_mixed_execution for execution in executions)
        before = await _pipeline_ids(db_session, account_id)
        repeated = await _upload(client, poisoned, _content(poisoned_rows, encoding), f"poisoned.{encoding}")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["data"]["raw_transactions_created"] == 0
        assert await _pipeline_ids(db_session, account_id) == before
        assert await _projection_signature(db_session, account_id) == expected


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_unknown_flip_marker_cannot_turn_overclose_into_new_short_position(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    baseline = await _create_context(client, primary_user)
    account_id = await _create_account(client, baseline.headers, baseline.portfolio_id)
    poisoned = _ImportContext(baseline.headers, baseline.user_id, baseline.portfolio_id, account_id)
    rows = [
        _execution(quantity="1", open_close="OPEN"),
        _execution(execution_id="overclose", side="SELL", quantity="2", open_close="CLOSE", executed_at="2026-03-14T11:31:07Z"),
    ]
    for context, payload in ((baseline, rows), (poisoned, [{**row, "openCloseIndicator": "C;O", "netCash": "not-cash"} for row in rows])):
        response = await _upload(client, context, _content(payload, encoding), f"fills.{encoding}")
        assert response.status_code == 200, response.text
    assert await _projection_signature(db_session, baseline.account_id) == await _projection_signature(db_session, account_id)
    trades = (await db_session.scalars(select(Trade).where(Trade.account_id == account_id))).all()
    assert trades and all(trade.closed_at is not None and trade.direction == Direction.LONG for trade in trades)
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == account_id))).all()
    assert all(not execution.is_mixed_execution for execution in executions)


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("asset_class", ["OPTION", "FUTURE"])
async def test_upload_generic_distinct_plain_derivative_contract_ids_create_independent_open_trades(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, asset_class: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [
        _option(asset_class=asset_class),
        _option(asset_class=asset_class, execution_id="other-contract", side="SELL", strike="155", instrument_id="call-155"),
    ]
    response = await _upload(client, context, _content(rows, encoding), f"fills.{encoding}")
    assert response.status_code == 200, response.text
    trades = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).all()
    assert len(trades) == 2
    assert all(trade.closed_at is None and not trade.is_combo for trade in trades)
    assert {trade.broker_conid for trade in trades} == {"call-150", "call-155"}
    assert {trade.direction for trade in trades} == {Direction.LONG, Direction.SHORT}
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    assert len({execution.trade_id for execution in executions}) == 2
    assert all(execution.trade_id is not None for execution in executions)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_standalone_occ_contract_without_id_matches_open_and_close_across_files(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    opening = _execution(asset_class="OPTION", symbol="AAPL  260619C00150000", quantity="1", price="3", multiplier="100", open_close="OPEN")
    closing = {
        **opening,
        "execution_id": "fill-close",
        "side": "SELL",
        "price": "4",
        "executed_at": "2026-03-14T11:31:07Z",
        "open_close": "CLOSE",
        "realized_pnl": "100",
    }
    for row in (opening, closing):
        response = await _upload(client, context, _content([row], encoding), f"fills.{encoding}")
        assert response.status_code == 200, response.text
    trade = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    assert trade.broker_conid is None
    assert trade.closed_at is not None
    assert trade.pnl == Decimal("100")
    assert trade.entry_price == Decimal("3")
    assert trade.exit_price == Decimal("4")
    state = await _pipeline_ids(db_session, context.account_id)
    assert len(state["raw"]) == len(state["executions"]) == 2
    for row in (opening, closing):
        repeated = await _upload(client, context, _content([row], encoding), f"fills.{encoding}")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["data"]["raw_transactions_created"] == 0
    assert await _pipeline_ids(db_session, context.account_id) == state


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("closing_pnl", [None, "profit", "zero"])
async def test_upload_generic_combo_closing_pnl_is_required_and_explicit_values_determine_result(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, closing_pnl: str | None
) -> None:
    context = await _create_context(client, primary_user)
    rows = _spread(closing_pnl=closing_pnl)
    response = await _upload(client, context, _content(rows, encoding), f"spread.{encoding}")
    if closing_pnl is None:
        errors = _validation_errors(response)
        locations = {4, 5} if encoding == "csv" else {"executions[2]", "executions[3]"}
        assert {error["location"] for error in errors if error["field"] == "realized_pnl"} == locations
        await _assert_no_pipeline_rows(db_session, context.account_id)
        return
    assert response.status_code == 200, response.text
    trade = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    assert trade.is_combo
    assert trade.closed_at == datetime(2026, 3, 14, 11, 31, 7, tzinfo=UTC)
    assert trade.pnl == (Decimal("50") if closing_pnl == "profit" else Decimal("0"))
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    assert len(executions) == 4
    assert all(execution.trade_id == trade.id for execution in executions)
    state = await _pipeline_ids(db_session, context.account_id)
    repeated = await _upload(client, context, _content(rows, encoding), f"spread.{encoding}")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["raw_transactions_created"] == 0
    assert await _pipeline_ids(db_session, context.account_id) == state


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_order_marker_conflict_with_persisted_partial_order_rejects_before_any_persistence(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    first = _option(order_id="same-order")
    initial = await _upload(client, context, _content([first], encoding), f"opening.{encoding}")
    assert initial.status_code == 200, initial.text
    before = await _pipeline_ids(db_session, context.account_id)
    rows = [
        _execution(execution_id="valid-but-not-persisted", symbol="MULTI\nLINE"),
        _option(execution_id="conflicting-leg", side="SELL", open_close="CLOSE", realized_pnl="0", order_id="same-order"),
    ]
    actions = [{"symbol": "SPLT", "date": "2026-03-13", "ratio": "4"}]
    content = _content(rows, encoding, corporate_actions=actions)
    for _ in range(2):
        response = await _upload(client, context, content, f"later.{encoding}")
        errors = _validation_errors(response)
        expected_location: int | str = 4 if encoding == "csv" else "executions[1]"
        assert {(error["location"], error["field"]) for error in errors} == {(expected_location, "open_close")}
        assert "same-order" not in str(errors)
        assert await _pipeline_ids(db_session, context.account_id) == before
        assert not (await db_session.scalars(select(CorporateAction.id).where(CorporateAction.user_id == context.user_id))).all()


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_matching_partial_order_markers_accept_later_leg_and_form_one_combo(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [
        _option(order_id="same-order"),
        _option(execution_id="other-leg", side="SELL", price="1", strike="155", instrument_id="call-155", order_id="same-order"),
    ]
    for row in rows:
        response = await _upload(client, context, _content([row], encoding), f"partial.{encoding}")
        assert response.status_code == 200, response.text
    trade = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    assert trade.is_combo
    assert trade.closed_at is None
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    assert len(executions) == 2
    assert all(execution.trade_id == trade.id for execution in executions)


@pytest.mark.parametrize("ratio", ["0.000000001", "1e-9", "10000000000", "99999999999.99999999"])
async def test_upload_generic_unrepresentable_split_ratio_rejects_all_execution_and_corporate_action_rows(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], ratio: str
) -> None:
    context = await _create_context(client, primary_user)
    actions = [{"symbol": "SPLT", "date": "2026-03-13", "ratio": "4"}, {"symbol": "OTHER", "date": "2026-03-13", "ratio": ratio}]
    response = await _upload(client, context, _content(_round_trip(), "json", corporate_actions=actions), "fills.json")
    errors = _validation_errors(response)
    assert {(error["location"], error["field"]) for error in errors} == {("corporate_actions[1]", "ratio")}
    await _assert_no_pipeline_rows(db_session, context.account_id)
    assert not (await db_session.scalars(select(CorporateAction.id).where(CorporateAction.user_id == context.user_id))).all()


@pytest.mark.parametrize("ratio", ["0.00000001", "9999999999.99999999"])
async def test_upload_generic_split_ratio_boundaries_are_persisted_exactly_without_rounding_to_zero(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], ratio: str
) -> None:
    context = await _create_context(client, primary_user)
    action = {"symbol": "SPLT", "date": "2026-03-13", "ratio": ratio}
    response = await _upload(client, context, _content(_round_trip(), "json", corporate_actions=[action]), "fills.json")
    assert response.status_code == 200, response.text
    stored = (await db_session.scalars(select(CorporateAction).where(CorporateAction.user_id == context.user_id))).one()
    assert stored.ratio == Decimal(ratio)
    assert stored.ratio > 0


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
async def test_upload_generic_utc_overflow_reports_all_invalid_rows_without_persistence_or_sensitive_details(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, timestamp: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [_execution(executed_at=timestamp), _execution(execution_id="other-invalid", quantity="0")]
    response = await _upload(client, context, _content(rows, encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    expected = {(2, "executed_at"), (3, "quantity")} if encoding == "csv" else {("executions[0]", "executed_at"), ("executions[1]", "quantity")}
    assert {(error["location"], error["field"]) for error in errors} == expected
    assert timestamp not in response.text
    await _assert_no_pipeline_rows(db_session, context.account_id)


_GENERIC_TRANSITIONS = [("generic", "ibkr"), ("generic", "schwab"), ("generic", "manual"), ("ibkr", "generic"), ("schwab", "generic"), ("manual", "generic")]


@pytest.mark.parametrize(("source_type", "target_type"), _GENERIC_TRANSITIONS)
@pytest.mark.parametrize("layer", ["raw", "logical", "trade"])
async def test_update_generic_namespace_with_any_history_layer_rejects_credentialless_transition_without_mutation(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], source_type: str, target_type: str, layer: str
) -> None:
    context = await _create_context(client, primary_user, source_type)
    await _seed_history(db_session, context, layer)
    before = await _pipeline_ids(db_session, context.account_id)
    response = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": target_type})
    assert response.status_code == 422, response.text
    assert "history" in str(response.json()["detail"]).lower()
    assert await db_session.scalar(select(Account.type).where(Account.id == context.account_id)) == source_type
    assert await _pipeline_ids(db_session, context.account_id) == before
    assert not (await db_session.scalars(select(AccountProperty.id).where(AccountProperty.account_id == context.account_id))).all()
    if layer == "raw":
        assert (await db_session.scalars(select(BrokerRawTransaction.suppressed).where(BrokerRawTransaction.account_id == context.account_id))).one() is True
    if layer == "logical":
        assert (await db_session.scalars(select(LogicalExecution.deleted).where(LogicalExecution.account_id == context.account_id))).one() is True


@pytest.mark.parametrize(("source_type", "target_type"), _GENERIC_TRANSITIONS)
async def test_update_generic_namespace_empty_credentialless_account_can_change_type(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], source_type: str, target_type: str
) -> None:
    context = await _create_context(client, primary_user, source_type)
    response = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": target_type})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["type"] == target_type
    assert response.json()["data"]["has_api_keys"] is False
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("account_type", ["generic", "ibkr", "schwab", "manual"])
@pytest.mark.parametrize("layer", ["raw", "logical", "trade"])
async def test_update_generic_namespace_same_type_preserves_existing_history_and_remains_allowed(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], account_type: str, layer: str
) -> None:
    context = await _create_context(client, primary_user, account_type)
    await _seed_history(db_session, context, layer)
    before = await _pipeline_ids(db_session, context.account_id)
    response = await client.patch(
        f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": account_type, "name": "Updated metadata"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["type"] == account_type
    assert await _pipeline_ids(db_session, context.account_id) == before


@pytest.mark.parametrize("native_type", ["ibkr", "schwab"])
async def test_update_generic_namespace_native_history_cannot_bypass_guard_via_manual_account(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], native_type: str
) -> None:
    context = await _create_context(client, primary_user, native_type)
    await _seed_history(db_session, context, "raw")
    before = await _pipeline_ids(db_session, context.account_id)
    intermediate = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": "manual"})
    assert intermediate.status_code == 200, intermediate.text
    response = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": "generic"})
    assert response.status_code == 422, response.text
    assert await db_session.scalar(select(Account.type).where(Account.id == context.account_id)) == "manual"
    assert await _pipeline_ids(db_session, context.account_id) == before


@pytest.mark.parametrize(("source_type", "target_type"), [("generic", "ibkr"), ("ibkr", "generic")])
async def test_update_generic_namespace_explicitly_cleared_account_can_switch_without_retained_raw_history(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], source_type: str, target_type: str
) -> None:
    context = await _create_context(client, primary_user, source_type)
    for layer in ("raw", "logical", "trade"):
        await _seed_history(db_session, context, layer)
    cleared = await client.delete(f"{context.accounts_url}/{context.account_id}/trades", headers=context.headers, params={"acknowledged": "true"})
    assert cleared.status_code == 200, cleared.text
    await _assert_no_pipeline_rows(db_session, context.account_id)
    response = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json={"type": target_type})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["type"] == target_type


async def _seed_history(session: AsyncSession, context: _ImportContext, layer: str) -> None:
    fields = {
        "user_id": context.user_id,
        "account_id": context.account_id,
        "symbol": "AAPL",
        "asset_class": AssetClass.EQUITY,
        "direction": Direction.LONG,
        "quantity": Decimal("1"),
        "commission": Decimal("0"),
        "currency": "USD",
    }
    if layer == "raw":
        row: BrokerRawTransaction | LogicalExecution | Trade = BrokerRawTransaction(
            **fields,
            id=f"{context.user_id}:history:{uuid4()}",
            broker_type="generic",
            price=Decimal("100"),
            multiplier=1,
            executed_at=datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC),
            suppressed=True,
            raw_data={},
        )
    elif layer == "logical":
        row = LogicalExecution(
            **fields,
            id=uuid4(),
            price=Decimal("100"),
            multiplier=1,
            executed_at=datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC),
            origin="manual",
            deleted=True,
        )
    else:
        assert layer == "trade"
        row = Trade(**fields, id=uuid4(), entry_price=Decimal("100"), opened_at=datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC))
    session.add(row)
    await session.flush()


def _validation_errors(response: Response) -> list[dict[str, Any]]:
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert set(detail) == {"message", "errors"}
    assert isinstance(detail["message"], str) and detail["message"]
    errors = detail["errors"]
    assert isinstance(errors, list) and errors
    for error in errors:
        assert set(error) == {"location", "field", "message"}
        assert isinstance(error["location"], (int, str))
        assert isinstance(error["field"], str) and error["field"]
        assert isinstance(error["message"], str) and error["message"]
    return errors


@contextmanager
def _capture_audit(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    # The real audit logger intentionally does not propagate to the root logger.
    logger = logging.getLogger("audit")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO):
            yield
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_round_trip_persists_raw_executions_and_closed_trade(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, _content(_round_trip(), encoding), f"fills.{encoding}")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["broker"] == "generic"
    assert result["filename"] == f"fills.{encoding}"
    assert result["raw_transactions_created"] == 2
    assert result["trades_created"] == 1
    assert isinstance(result["warnings"], list)
    state = await _pipeline_ids(db_session, context.account_id)
    assert state["raw"] == {f"{context.user_id}:generic:{context.account_id}:fill-open", f"{context.user_id}:generic:{context.account_id}:fill-close"}
    assert len(state["executions"]) == 2
    assert len(state["trades"]) == 1
    trade = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    assert trade.symbol == "AAPL"
    assert trade.asset_class == AssetClass.EQUITY
    assert trade.direction == Direction.LONG
    assert trade.quantity == Decimal("2")
    assert trade.entry_price == Decimal("100")
    assert trade.exit_price == Decimal("110")
    assert trade.pnl == Decimal("20")
    assert trade.commission == Decimal("2")
    assert trade.opened_at == datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC)
    assert trade.closed_at == datetime(2026, 3, 14, 11, 31, 7, tzinfo=UTC)
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    assert all(execution.trade_id == trade.id for execution in executions)


async def test_upload_generic_json_numeric_literals_reach_database_as_exact_decimals(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    row = _execution(price="0.1", quantity="3", commission="0.00000001")
    content = _content([row], "json").replace(b'"0.1"', b"0.1").replace(b'"3"', b"3").replace(b'"0.00000001"', b"0.00000001")
    response = await _upload(client, context, content, "fills.json")
    assert response.status_code == 200, response.text
    raw = (await db_session.scalars(select(BrokerRawTransaction).where(BrokerRawTransaction.account_id == context.account_id))).one()
    assert raw.price == Decimal("0.1")
    assert raw.quantity == Decimal("3")
    assert raw.commission == Decimal("0.00000001")
    assert _source_record(raw)["price"] == "0.1"


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_orphan_close_without_reported_pnl_reconstructs_a_closed_trade(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    row = _execution(side="SELL", open_close="CLOSE")
    response = await _upload(client, context, _content([row], encoding), f"fills.{encoding}")
    assert response.status_code == 200, response.text
    raw = (await db_session.scalars(select(BrokerRawTransaction).where(BrokerRawTransaction.account_id == context.account_id))).one()
    assert raw.pnl == Decimal("0")
    assert raw.open_close == "C"
    assert raw.executed_at == datetime(2026, 3, 14, 10, 31, 7, tzinfo=UTC)
    trade = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).one()
    assert trade.opened_at == raw.executed_at
    assert trade.closed_at is not None and trade.closed_at > trade.opened_at
    assert trade.exit_price == Decimal("100")
    assert trade.quantity == Decimal("2")
    assert trade.direction == Direction.LONG


@pytest.mark.parametrize(("encoding", "filename"), [("json", "fills.csv"), ("csv", "fills.json")])
async def test_upload_generic_encoding_is_selected_by_content_not_extension(
    client: AsyncClient, primary_user: dict[str, Any], encoding: str, filename: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, _content(_round_trip(), encoding), filename)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["raw_transactions_created"] == 2


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("changed_values", [False, True])
async def test_upload_generic_reimport_is_noop_and_never_updates_stored_raw_values(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, changed_values: bool
) -> None:
    context = await _create_context(client, primary_user)
    rows = _round_trip()
    first = await _upload(client, context, _content(rows, encoding), f"first.{encoding}")
    assert first.status_code == 200, first.text
    before = await _pipeline_ids(db_session, context.account_id)
    if changed_values:
        rows = [{**row, "price": "999.999", "quantity": "9", "commission": "5"} for row in rows]
    second = await _upload(client, context, _content(rows, encoding), f"second.{encoding}")
    assert second.status_code == 200, second.text
    assert second.json()["data"]["raw_transactions_created"] == 0
    assert second.json()["data"]["trades_created"] == 0
    assert await _pipeline_ids(db_session, context.account_id) == before
    raw = (
        await db_session.scalars(
            select(BrokerRawTransaction).where(BrokerRawTransaction.account_id == context.account_id).execution_options(populate_existing=True)
        )
    ).all()
    assert {row.price for row in raw} == {Decimal("100"), Decimal("110")}
    assert all(row.quantity == Decimal("2") and row.commission == Decimal("1") for row in raw)
    assert {_source_record(row)["price"] for row in raw} == {"100", "110"}


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_overlapping_exports_adds_only_unseen_execution_identifiers(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    first_rows = [_execution(execution_id=str(index), quantity="1") for index in range(1, 51)]
    second_rows = [_execution(execution_id=str(index), quantity="1", price="999" if index <= 50 else "100") for index in range(40, 91)]
    first = await _upload(client, context, _content(first_rows, encoding), f"first.{encoding}")
    assert first.status_code == 200, first.text
    assert first.json()["data"]["raw_transactions_created"] == 50
    second = await _upload(client, context, _content(second_rows, encoding), f"second.{encoding}")
    assert second.status_code == 200, second.text
    assert second.json()["data"]["raw_transactions_created"] == 40
    state = await _pipeline_ids(db_session, context.account_id)
    assert state["raw"] == {f"{context.user_id}:generic:{context.account_id}:{index}" for index in range(1, 91)}
    assert len(state["executions"]) == 90
    raw_prices = (await db_session.scalars(select(BrokerRawTransaction.price).where(BrokerRawTransaction.account_id == context.account_id))).all()
    assert set(raw_prices) == {Decimal("100")}


@pytest.mark.parametrize("execution_id", ["1", "x" * 100])
async def test_upload_generic_same_execution_identifier_in_two_accounts_keeps_distinct_raw_namespaces(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], execution_id: str
) -> None:
    first = await _create_context(client, primary_user)
    other_account = await _create_account(client, first.headers, first.portfolio_id)
    second = _ImportContext(first.headers, first.user_id, first.portfolio_id, other_account)
    for context in (first, second):
        response = await _upload(client, context, _content([_execution(execution_id=execution_id)]))
        assert response.status_code == 200, response.text
        assert response.json()["data"]["raw_transactions_created"] == 1
        state = await _pipeline_ids(db_session, context.account_id)
        assert state["raw"] == {f"{context.user_id}:generic:{context.account_id}:{execution_id}"}
        assert len(state["executions"]) == 1
        assert len(state["trades"]) == 1


async def test_upload_generic_unknown_csv_column_is_reported_once_without_its_values(client: AsyncClient, primary_user: dict[str, Any]) -> None:
    context = await _create_context(client, primary_user)
    rows = [{**row, "broker_note": "PRIVATE_NOTE_837462"} for row in _round_trip()]
    response = await _upload(client, context, _content(rows))
    assert response.status_code == 200, response.text
    warnings = response.json()["data"]["warnings"]
    assert sum("broker_note" in warning for warning in warnings) == 1
    assert "PRIVATE_NOTE_837462" not in str(warnings)


@pytest.mark.parametrize("account_type", ["ibkr", "schwab", "manual"])
async def test_upload_generic_csv_to_non_generic_account_returns_422_without_persistence(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], account_type: str
) -> None:
    context = await _create_context(client, primary_user)
    account_id = await _create_account(client, context.headers, context.portfolio_id, account_type)
    target = _ImportContext(context.headers, context.user_id, context.portfolio_id, account_id)
    response = await _upload(client, target, _content(_round_trip()))
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    await _assert_no_pipeline_rows(db_session, account_id)


@pytest.mark.parametrize("filename", ["fills.xml", "fills.txt", "no-extension"])
async def test_upload_generic_unsupported_extension_returns_422(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], filename: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, b"<?xml version='1.0'?><trades/>", filename)
    assert response.status_code == 422, response.text
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("filename", ["fills.csv", "fills.json"])
async def test_upload_generic_xml_content_with_allowed_extension_returns_400_not_a_generic_validation_object(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], filename: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, b"\xef\xbb\xbf \n<?xml version='1.0'?><trades/>", filename)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "XML" in detail and "CSV" in detail and "JSON" in detail
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_two_invalid_rows_reports_every_location_and_persists_nothing(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [_execution(execution_id=f"fill-{index}") for index in range(99)]
    rows[10]["quantity"] = "0"
    rows[55]["executed_at"] = "2026-03-14T10:31:07"
    response = await _upload(client, context, _content(rows, encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    expected = {(12, "quantity"), (57, "executed_at")} if encoding == "csv" else {("executions[10]", "quantity"), ("executions[55]", "executed_at")}
    assert {(error["location"], error["field"]) for error in errors} == expected
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_invalid_document_leaves_existing_projection_untouched(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    first = await _upload(client, context, _content(_round_trip(), encoding), f"first.{encoding}")
    assert first.status_code == 200, first.text
    before = await _pipeline_ids(db_session, context.account_id)
    rows = [_execution(execution_id="new-valid"), _execution(execution_id="new-invalid", quantity="0")]
    response = await _upload(client, context, _content(rows, encoding), f"invalid.{encoding}")
    _validation_errors(response)
    assert await _pipeline_ids(db_session, context.account_id) == before


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_duplicate_id_with_invalid_row_reports_both_occurrences_and_persists_nothing(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, _content([_execution(), _execution(quantity="0")], encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    assert {error["location"] for error in errors if error["field"] == "execution_id"} == ({2, 3} if encoding == "csv" else {"executions[0]", "executions[1]"})
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_empty_execution_document_returns_structured_400(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, _content([], encoding), f"empty.{encoding}")
    errors = _validation_errors(response)
    assert any("no executions" in error["message"].lower() for error in errors)
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize(("content", "filename"), [(b'{"schema_version":', "fills.json"), (b'"unterminated', "fills.csv"), (b"\xff\xfe", "fills.csv")])
async def test_upload_generic_malformed_payload_returns_structured_400_without_partial_persistence(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], content: bytes, filename: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, content, filename)
    _validation_errors(response)
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("version", ["2.0", "10", 1, None])
async def test_upload_generic_unsupported_json_schema_version_returns_supported_version_error(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], version: object
) -> None:
    context = await _create_context(client, primary_user)
    response = await _upload(client, context, _content([_execution()], "json", schema_version=version), "fills.json")
    errors = _validation_errors(response)
    assert {error["field"] for error in errors} == {"schema_version"}
    assert "'1'" in errors[0]["message"]
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_many_errors_keeps_first_200_and_appends_exact_omitted_count(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [_execution(execution_id=f"fill-{index}", quantity="0") for index in range(GENERIC_IMPORT_MAX_ERRORS + 7)]
    response = await _upload(client, context, _content(rows, encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    assert len(errors) == GENERIC_IMPORT_MAX_ERRORS + 1
    expected_locations = (
        list(range(2, GENERIC_IMPORT_MAX_ERRORS + 2)) if encoding == "csv" else [f"executions[{index}]" for index in range(GENERIC_IMPORT_MAX_ERRORS)]
    )
    assert [error["location"] for error in errors[:-1]] == expected_locations
    assert all(error["field"] == "quantity" for error in errors[:-1])
    assert "7 more" in errors[-1]["message"]
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_exactly_200_errors_does_not_add_a_truncation_marker(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    rows = [_execution(execution_id=f"fill-{index}", quantity="0") for index in range(GENERIC_IMPORT_MAX_ERRORS)]
    response = await _upload(client, context, _content(rows, encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    assert len(errors) == GENERIC_IMPORT_MAX_ERRORS
    assert all(error["field"] == "quantity" for error in errors)
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("valid", [False, True])
async def test_upload_generic_audits_request_and_never_logs_sensitive_execution_values(
    client: AsyncClient, primary_user: dict[str, Any], caplog: pytest.LogCaptureFixture, encoding: str, valid: bool
) -> None:
    context = await _create_context(client, primary_user)
    values = {"price": "1234567.87654321", "quantity": "7654321.12345678", "commission": "17.12345678", "realized_pnl": "-8765432.23456789"}
    row = _execution(**values, side="BUY" if valid else "PRIVATE_SIDE_837462", open_close="CLOSE")
    with _capture_audit(caplog):
        response = await _upload(client, context, _content([row], encoding), f"fills.{encoding}")
    assert response.status_code == (200 if valid else 400), response.text
    if not valid:
        errors = _validation_errors(response)
        assert {error["field"] for error in errors} == {"side"}
        assert "input" not in json.dumps(errors)
        assert "ctx" not in json.dumps(errors)
        for value in (*values.values(), "PRIVATE_SIDE_837462"):
            assert value not in response.text
    log_data = caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    for value in (*values.values(), "PRIVATE_SIDE_837462", context.headers["Authorization"]):
        assert value not in log_data
    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == "audit"]
    matching = [event for event in events if event["path"] == context.upload_url and event["method"] == "POST"]
    assert len(matching) == 1
    assert matching[0]["user_id"] == str(context.user_id)
    assert matching[0]["status_code"] == response.status_code
    assert matching[0]["action"].startswith("import.")


@pytest.mark.parametrize("encoding", ["csv", "json"])
async def test_upload_generic_invalid_numeric_and_datetime_values_never_escape_validation_errors_or_logs(
    client: AsyncClient, primary_user: dict[str, Any], caplog: pytest.LogCaptureFixture, encoding: str
) -> None:
    context = await _create_context(client, primary_user)
    changes = {
        field: f"PRIVATE_{field.upper()}_918273" for field in ("price", "quantity", "commission", "realized_pnl", "executed_at", "expiry", "option_type")
    }
    with _capture_audit(caplog):
        response = await _upload(client, context, _content([_execution(**changes)], encoding), f"fills.{encoding}")
    errors = _validation_errors(response)
    assert {error["field"] for error in errors} == set(changes)
    diagnostics = response.text + caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    for value in changes.values():
        assert value not in diagnostics


async def test_upload_generic_invalid_corporate_action_rolls_back_all_document_data(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    actions = [{"symbol": "AAPL", "date": "2026-03-13", "ratio": "4"}, {"symbol": "MSFT", "date": "2026-03-13", "ratio": "0"}]
    response = await _upload(client, context, _content(_round_trip(), "json", corporate_actions=actions), "fills.json")
    errors = _validation_errors(response)
    assert {(error["location"], error["field"]) for error in errors} == {("corporate_actions[1]", "ratio")}
    assert not (await db_session.scalars(select(CorporateAction.id).where(CorporateAction.user_id == context.user_id))).all()
    await _assert_no_pipeline_rows(db_session, context.account_id)


async def test_upload_generic_json_corporate_action_is_persisted_by_unified_import_pipeline(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    actions = [{"symbol": "SPLT", "date": "2026-03-13", "ratio": 4, "old_instrument_id": "before", "new_instrument_id": "after"}]
    response = await _upload(client, context, _content(_round_trip(), "json", corporate_actions=actions), "fills.json")
    assert response.status_code == 200, response.text
    action = (await db_session.scalars(select(CorporateAction).where(CorporateAction.user_id == context.user_id))).one()
    assert action.symbol == "SPLT"
    assert action.ratio == Decimal("4")
    assert action.old_conid == "before"
    assert action.new_conid == "after"


async def test_upload_generic_multileg_without_identifiers_is_rejected_before_persistence(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    first = _execution(asset_class="OPTION", multiplier="100", expiry="2026-06-19", strike="150", option_type="C", order_id="order-77", open_close="OPEN")
    second = {**first, "execution_id": "other-leg", "strike": "155", "side": "SELL"}
    response = await _upload(client, context, _content([first, second]))
    errors = _validation_errors(response)
    assert {(error["location"], error["field"]) for error in errors} == {(2, "instrument_id"), (3, "instrument_id")}
    await _assert_no_pipeline_rows(db_session, context.account_id)


async def test_upload_generic_without_authentication_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/portfolios/{uuid4()}/imports/upload",
        data={"account_id": str(uuid4())},
        files={"file": ("fills.csv", _content(_round_trip()), "text/csv")},
    )
    assert response.status_code == 401, response.text


async def test_upload_generic_missing_account_field_returns_422_with_field_detail(client: AsyncClient, primary_user: dict[str, Any]) -> None:
    context = await _create_context(client, primary_user)
    response = await client.post(context.upload_url, headers=context.headers, files={"file": ("fills.csv", _content(_round_trip()), "text/csv")})
    assert response.status_code == 422, response.text
    assert any(error["loc"][-1] == "account_id" for error in response.json()["detail"])


async def test_upload_generic_unknown_account_returns_404(client: AsyncClient, primary_user: dict[str, Any]) -> None:
    context = await _create_context(client, primary_user)
    missing = _ImportContext(context.headers, context.user_id, context.portfolio_id, uuid4())
    response = await _upload(client, missing, _content(_round_trip()))
    assert response.status_code == 404, response.text


async def test_upload_generic_account_in_other_portfolio_is_hidden_with_404(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    different_portfolio = await _create_portfolio(client, context.headers)
    wrong_scope = _ImportContext(context.headers, context.user_id, different_portfolio, context.account_id)
    response = await _upload(client, wrong_scope, _content(_round_trip()))
    assert response.status_code == 404, response.text
    await _assert_no_pipeline_rows(db_session, context.account_id)


async def test_upload_generic_another_users_portfolio_is_hidden_with_404(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], other_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    unauthorized = _ImportContext(other_user["headers"], UUID(other_user["user_id"]), context.portfolio_id, context.account_id)
    response = await _upload(client, unauthorized, _content(_round_trip()))
    assert response.status_code == 404, response.text
    await _assert_no_pipeline_rows(db_session, context.account_id)


@pytest.mark.parametrize("properties", [None, {}])
async def test_create_generic_account_inherits_portfolio_currency_and_stores_no_credentials(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], properties: dict[str, str] | None
) -> None:
    portfolio_id = await _create_portfolio(client, primary_user["headers"], "EUR")
    response = await client.post(
        f"/api/v1/portfolios/{portfolio_id}/accounts",
        headers=primary_user["headers"],
        json={"name": "Other broker", "type": "generic", "currency": "USD", "properties": properties},
    )
    assert response.status_code == 201, response.text
    account = response.json()["data"]
    assert account["type"] == "generic"
    assert account["currency"] == "EUR"
    assert account["portfolio_id"] == str(portfolio_id)
    assert account["has_api_keys"] is False
    assert not (await db_session.scalars(select(AccountProperty.id).where(AccountProperty.account_id == UUID(account["id"])))).all()


async def test_create_generic_account_with_credentials_returns_422_and_stores_nothing(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    portfolio_id = await _create_portfolio(client, primary_user["headers"])
    response = await client.post(
        f"/api/v1/portfolios/{portfolio_id}/accounts",
        headers=primary_user["headers"],
        json={"name": "Forbidden credential account", "type": "generic", "properties": {"ibkr_flex_token": "PRIVATE_CREDENTIAL_918273"}},
    )
    assert response.status_code == 422, response.text
    assert not (await db_session.scalars(select(Account.id).where(Account.portfolio_id == portfolio_id))).all()
    assert not (await db_session.scalars(select(AccountProperty.id).join(Account).where(Account.portfolio_id == portfolio_id))).all()


@pytest.mark.parametrize("include_type", [False, True])
async def test_update_generic_account_with_credentials_returns_422_without_storing_properties(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], include_type: bool
) -> None:
    context = await _create_context(client, primary_user)
    payload: dict[str, object] = {"properties": {"schwab_app_secret": "PRIVATE_CREDENTIAL_746382"}}
    if include_type:
        payload["type"] = "generic"
    response = await client.patch(f"{context.accounts_url}/{context.account_id}", headers=context.headers, json=payload)
    assert response.status_code == 422, response.text
    assert not (await db_session.scalars(select(AccountProperty.id).where(AccountProperty.account_id == context.account_id))).all()


async def test_update_broker_account_to_generic_with_existing_credentials_is_rejected(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    portfolio_id = await _create_portfolio(client, primary_user["headers"])
    created = await client.post(
        f"/api/v1/portfolios/{portfolio_id}/accounts",
        headers=primary_user["headers"],
        json={"name": "Existing broker", "type": "ibkr", "properties": {"ibkr_flex_token": "PRIVATE_CREDENTIAL_192837"}},
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["data"]["id"]
    response = await client.patch(f"/api/v1/portfolios/{portfolio_id}/accounts/{account_id}", headers=primary_user["headers"], json={"type": "generic"})
    assert response.status_code == 422, response.text
    stored_type = await db_session.scalar(select(Account.type).where(Account.id == UUID(account_id)))
    assert stored_type == "ibkr"


async def test_get_generic_account_credentials_returns_422(client: AsyncClient, primary_user: dict[str, Any]) -> None:
    context = await _create_context(client, primary_user)
    response = await client.get(f"{context.accounts_url}/{context.account_id}/credentials", headers=context.headers)
    assert response.status_code == 422, response.text
    assert "credentials" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/v1/broker/sync/{account_id}", None),
        ("POST", "/api/v1/broker/schwab/authorize/{account_id}", None),
        ("POST", "/api/v1/broker/schwab/complete/{account_id}", {"redirect_url": "https://example.test/?code=unused&state=unused"}),
        ("GET", "/api/v1/broker/schwab/status/{account_id}", None),
        ("DELETE", "/api/v1/broker/schwab/connect/{account_id}", None),
    ],
)
async def test_generic_account_broker_api_paths_reject_without_creating_credentials(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], method: str, path: str, payload: dict[str, str] | None
) -> None:
    context = await _create_context(client, primary_user)
    response = await client.request(method, path.format(account_id=context.account_id), headers=context.headers, json=payload)
    assert response.status_code == 400, response.text
    assert "generic" in response.json()["detail"].lower()
    assert not (await db_session.scalars(select(AccountProperty.id).where(AccountProperty.account_id == context.account_id))).all()


@pytest.mark.parametrize("claimed_type", ["generic", "ibkr", "schwab"])
async def test_generic_account_connection_test_cannot_impersonate_a_native_broker(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], claimed_type: str
) -> None:
    context = await _create_context(client, primary_user)
    response = await client.post(
        f"{context.accounts_url}/test-connection",
        headers=context.headers,
        json={"type": claimed_type, "account_id": str(context.account_id), "properties": {"ibkr_flex_token": "unused"}},
    )
    assert response.status_code == (422 if claimed_type == "generic" else 400), response.text
    assert not (await db_session.scalars(select(AccountProperty.id).where(AccountProperty.account_id == context.account_id))).all()


async def test_create_generic_account_under_foreign_portfolio_returns_404(
    client: AsyncClient, primary_user: dict[str, Any], other_user: dict[str, Any]
) -> None:
    portfolio_id = await _create_portfolio(client, other_user["headers"])
    response = await client.post(f"/api/v1/portfolios/{portfolio_id}/accounts", headers=primary_user["headers"], json={"name": "Not mine", "type": "generic"})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("path", ["/schema", "/template.csv"])
async def test_generic_download_without_authentication_returns_401(client: AsyncClient, path: str) -> None:
    response = await client.get(f"{_DOWNLOAD_PREFIX}{path}")
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("path", ["/schema", "/template.csv"])
async def test_generic_download_authenticated_user_without_any_portfolio_is_allowed(client: AsyncClient, other_user: dict[str, Any], path: str) -> None:
    response = await client.get(f"{_DOWNLOAD_PREFIX}{path}", headers=other_user["headers"])
    assert response.status_code == 200, response.text


async def test_generic_schema_download_describes_every_field_required_flags_types_and_conditional_rules(
    client: AsyncClient, primary_user: dict[str, Any]
) -> None:
    response = await client.get(f"{_DOWNLOAD_PREFIX}/schema", headers=primary_user["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "application/schema+json"
    schema = response.json()
    assert schema == GenericImportDocumentV1.model_json_schema()
    assert set(schema["required"]) == {"schema_version", "executions"}
    execution_schema = schema["$defs"]["GenericExecutionV1"]
    assert list(execution_schema["properties"]) == list(GenericExecutionV1.model_fields)
    assert set(execution_schema["required"]) == _REQUIRED_FIELDS
    for field in execution_schema["properties"].values():
        assert any(key in field for key in ("type", "anyOf", "$ref"))
        assert field["description"]
    assert "OPTION" in execution_schema["properties"]["multiplier"]["description"]
    assert "FUTURE" in execution_schema["properties"]["multiplier"]["description"]
    for field in ("expiry", "strike", "option_type"):
        assert "OPTION" in execution_schema["properties"][field]["description"]
        assert "OCC" in execution_schema["properties"][field]["description"]
    assert "multi-leg" in execution_schema["properties"]["instrument_id"]["description"]
    assert set(schema["$defs"]["GenericCorporateActionV1"]["required"]) == {"symbol", "date", "ratio"}


async def test_generic_csv_template_has_exact_header_equity_round_trip_and_identified_option_spread(client: AsyncClient, primary_user: dict[str, Any]) -> None:
    response = await client.get(f"{_DOWNLOAD_PREFIX}/template.csv", headers=primary_user["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "text/csv"
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    assert reader.fieldnames == list(GenericExecutionV1.model_fields)
    rows = list(reader)
    equity_rows = [row for row in rows if row["asset_class"] == "EQUITY"]
    assert len(equity_rows) >= 2
    assert len({row["symbol"] for row in equity_rows}) == 1
    assert {row["side"] for row in equity_rows} == {"BUY", "SELL"}
    assert {row["open_close"] for row in equity_rows} == {"OPEN", "CLOSE"}
    orders: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["asset_class"] == "OPTION":
            assert row["order_id"]
            assert row["instrument_id"]
            orders[row["order_id"]].append(row)
    assert orders
    for legs in orders.values():
        contracts = {(leg["symbol"], leg["expiry"], leg["strike"], leg["option_type"]) for leg in legs}
        assert len(contracts) >= 2
        assert len({leg["instrument_id"] for leg in legs}) == len(contracts)
        assert len({leg["open_close"] for leg in legs}) == 1
        assert all(leg["open_close"] for leg in legs)


async def test_generic_csv_template_upload_to_empty_account_creates_closed_equity_and_one_combo(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    await _assert_no_pipeline_rows(db_session, context.account_id)
    download = await client.get(f"{_DOWNLOAD_PREFIX}/template.csv", headers=context.headers)
    assert download.status_code == 200, download.text
    rows = list(csv.DictReader(io.StringIO(download.text)))
    response = await _upload(client, context, download.content, "downloaded-template.csv")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["raw_transactions_created"] == len(rows)
    state = await _pipeline_ids(db_session, context.account_id)
    assert len(state["raw"]) == len(rows)
    assert len(state["executions"]) == len(rows)
    trades = (await db_session.scalars(select(Trade).where(Trade.account_id == context.account_id))).all()
    assert len(trades) == 2
    assert sum(trade.asset_class == AssetClass.EQUITY and trade.closed_at is not None for trade in trades) == 1
    assert sum(trade.is_combo for trade in trades) == 1
    executions = (await db_session.scalars(select(LogicalExecution).where(LogicalExecution.account_id == context.account_id))).all()
    assert all(execution.trade_id is not None for execution in executions)


async def test_generic_account_import_lock_wins_before_namespace_transition_and_history_blocks_retry(engine: object, setup_database: None) -> None:
    async with _committed_test_account(engine) as (factory, context), factory() as importing, factory() as changing:
        result = await ImportService(importing).import_trades(
            _content([_execution()], "json"), "fills.json", BrokerType.GENERIC, context.portfolio_id, context.account_id, context.user_id
        )
        assert result.raw_transactions_created == 1
        await changing.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as caught:
            await AccountService(changing).update_account(context.account_id, AccountUpdate(type=AccountType.IBKR), context.user_id, context.portfolio_id)
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await changing.rollback()
        await importing.commit()
        with pytest.raises(AccountValidationError, match="history"):
            await AccountService(changing).update_account(context.account_id, AccountUpdate(type=AccountType.IBKR), context.user_id, context.portfolio_id)
        assert await changing.scalar(select(Account.type).where(Account.id == context.account_id)) == AccountType.GENERIC
        assert {name: len(ids) for name, ids in (await _pipeline_ids(changing, context.account_id)).items()} == {"raw": 1, "executions": 1, "trades": 1}


async def test_generic_account_namespace_transition_lock_wins_before_import_and_old_namespace_is_rejected(engine: object, setup_database: None) -> None:
    async with _committed_test_account(engine) as (factory, context), factory() as changing, factory() as importing:
        result = await AccountService(changing).update_account(context.account_id, AccountUpdate(type=AccountType.IBKR), context.user_id, context.portfolio_id)
        assert result.type == AccountType.IBKR
        await importing.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as caught:
            await ImportService(importing).import_trades(
                _content([_execution()], "json"), "fills.json", BrokerType.GENERIC, context.portfolio_id, context.account_id, context.user_id
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await importing.rollback()
        await changing.commit()
        with pytest.raises(ImportNotAllowedError, match="generic accounts"):
            await ImportService(importing).import_trades(
                _content([_execution()], "json"), "fills.json", BrokerType.GENERIC, context.portfolio_id, context.account_id, context.user_id
            )
        await _assert_no_pipeline_rows(importing, context.account_id)
        assert await importing.scalar(select(Account.type).where(Account.id == context.account_id)) == AccountType.IBKR


async def test_generic_account_locked_lookup_refreshes_preloaded_stale_type_before_import(engine: object, setup_database: None) -> None:
    async with _committed_test_account(engine) as (factory, context), factory() as cached_session, factory() as writer:
        cached = await AccountRepository(cached_session).get_by_id(context.account_id, context.user_id, context.portfolio_id)
        assert cached is not None and cached.type == AccountType.GENERIC
        await AccountService(writer).update_account(context.account_id, AccountUpdate(type=AccountType.IBKR), context.user_id, context.portfolio_id)
        await writer.commit()
        assert cached.type == AccountType.GENERIC
        with pytest.raises(ImportNotAllowedError, match="generic accounts"):
            await ImportService(cached_session).import_trades(
                _content([_execution()], "json"), "fills.json", BrokerType.GENERIC, context.portfolio_id, context.account_id, context.user_id
            )
        assert str(cached.type) == AccountType.IBKR.value
        await _assert_no_pipeline_rows(cached_session, context.account_id)


async def test_generic_account_locked_lookup_refreshes_preloaded_empty_credential_collection(engine: object, setup_database: None) -> None:
    async with _committed_test_account(engine, AccountType.IBKR) as (factory, context), factory() as cached_session, factory() as writer:
        cached = await AccountRepository(cached_session).get_by_id_with_properties(context.account_id, context.user_id, context.portfolio_id)
        assert cached is not None and cached.properties == []
        await AccountService(writer).update_account(
            context.account_id, AccountUpdate(properties={"api_secret": "temporary-test-secret"}), context.user_id, context.portfolio_id
        )
        await writer.commit()
        assert cached.properties == []
        with pytest.raises(AccountValidationError, match="credentials"):
            await AccountService(cached_session).update_account(
                context.account_id, AccountUpdate(type=AccountType.GENERIC), context.user_id, context.portfolio_id
            )
        assert cached.type == AccountType.IBKR
        assert len(cached.properties) == 1
        assert cached.properties[0].value != "temporary-test-secret"
        await _assert_no_pipeline_rows(cached_session, context.account_id)


async def test_generic_schema_download_patterns_match_the_same_case_and_multiplier_forms_accepted_by_upload(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any]
) -> None:
    context = await _create_context(client, primary_user)
    downloaded = await client.get(f"{_DOWNLOAD_PREFIX}/schema", headers=context.headers)
    assert downloaded.status_code == 200, downloaded.text
    execution_schema = downloaded.json()["$defs"]["GenericExecutionV1"]
    properties = execution_schema["properties"]
    for field, variants in {
        "asset_class": ["equity", "oPtIoN", " fUtUrE "],
        "side": ["buy", "sElL"],
        "open_close": [" oPeN ", "close"],
        "option_type": ["c", " P "],
    }.items():
        branches = properties[field].get("anyOf", [properties[field]])
        patterns = [branch["pattern"] for branch in branches if "pattern" in branch]
        assert patterns and all("(?i)" not in pattern for pattern in patterns)
        assert all(any(re.fullmatch(pattern, value) for pattern in patterns) for value in variants)
    multiplier_branches = properties["multiplier"]["anyOf"]
    assert any(branch.get("type") == "integer" and branch["minimum"] == 1 for branch in multiplier_branches)
    string_patterns = [branch["pattern"] for branch in multiplier_branches if branch.get("type") == "string"]
    assert all(any(re.fullmatch(pattern, value) for pattern in string_patterns) for value in ("100", "+100", "100.0", " 100 "))
    assert all(not any(re.fullmatch(pattern, value) for pattern in string_patterns) for value in ("0", "-1", "1.5", "true"))
    assert execution_schema["allOf"]
    conditions = json.dumps(execution_schema["allOf"])
    assert all(field in conditions for field in ("instrument_id", "multiplier", "order_id", "open_close", "realized_pnl"))
    rows = [
        _execution(execution_id="mixed-equity", asset_class="eQuItY", side="bUy", multiplier="100"),
        _option(execution_id="mixed-option", asset_class="oPtIoN", option_type="c", open_close="oPeN", multiplier="100.0"),
        _execution(execution_id="mixed-future", asset_class="fUtUrE", side="sElL", multiplier=100, instrument_id="future-contract"),
    ]
    response = await _upload(client, context, _content(rows, "json"), "forms.json")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["raw_transactions_created"] == len(rows)
    assert len((await _pipeline_ids(db_session, context.account_id))["raw"]) == len(rows)


@pytest.mark.parametrize("encoding", ["csv", "json"])
@pytest.mark.parametrize("scenario", ["plain-option-id", "future-id", "single-order-id", "single-order-marker", "open-pnl"])
async def test_upload_generic_strict_contract_failures_reject_before_any_raw_or_projected_rows(
    client: AsyncClient, db_session: AsyncSession, primary_user: dict[str, Any], encoding: str, scenario: str
) -> None:
    context = await _create_context(client, primary_user)
    row = _option()
    expected_field = "instrument_id"
    if scenario == "plain-option-id":
        row.pop("instrument_id")
    elif scenario == "future-id":
        row.update(asset_class="FUTURE")
        row.pop("instrument_id")
    elif scenario == "single-order-id":
        row.update(symbol="AAPL  260619C00150000", order_id="public-order")
        row.pop("instrument_id")
    elif scenario == "single-order-marker":
        row.update(order_id="public-order")
        row.pop("open_close")
        expected_field = "open_close"
    else:
        row["realized_pnl"] = "0"
        expected_field = "realized_pnl"
    response = await _upload(client, context, _content([row], encoding), f"invalid.{encoding}")
    errors = _validation_errors(response)
    assert {error["field"] for error in errors} == {expected_field}
    await _assert_no_pipeline_rows(db_session, context.account_id)
