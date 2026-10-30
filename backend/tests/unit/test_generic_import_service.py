"""Generic pipeline guards and ID stamping with every repository mocked."""

import json
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, call, patch
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import AccountNotFoundError, AccountValidationError, GenericImportValidationError, ImportNotAllowedError
from app.models.account import Account, AccountType
from app.repositories.account_repository import AccountRepository
from app.schemas.account import AccountUpdate
from app.schemas.broker_raw_transaction import BrokerRawTransactionCreate
from app.schemas.imports import ImportResult
from app.services.account_service import AccountService
from app.services.import_service import ImportService
from app.utils.parsers.factory import BrokerType

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _service(session: AsyncMock, account_type: AccountType | None) -> tuple[ImportService, dict[str, AsyncMock]]:
    service = ImportService(session)
    repositories = {name: AsyncMock() for name in ("_account_repo", "_portfolio_repo", "_ca_repo", "_trade_repo", "_raw_repo", "_exec_repo", "_logical_svc")}
    for name, repository in repositories.items():
        setattr(service, name, repository)
    account = Account(type=account_type) if account_type is not None else None
    repositories["_account_repo"].get_by_id.return_value = account
    repositories["_account_repo"].has_trading_history.return_value = False
    repositories["_portfolio_repo"].get_by_id.return_value = None
    repositories["_ca_repo"].list_by_user.return_value = []
    trade_repo = repositories["_trade_repo"]
    for method in ("list_open_lots_by_user", "list_orphaned_lots_by_user", "list_open_combo_positions_by_user"):
        getattr(trade_repo, method).return_value = []
    for method in ("list_manually_edited_keys", "list_existing_entry_keys", "list_existing_close_keys"):
        getattr(trade_repo, method).return_value = set()
    repositories["_raw_repo"].bulk_upsert.return_value = []
    repositories["_raw_repo"].list_released_without_execution.return_value = []
    repositories["_logical_svc"].bulk_create_from_raw.return_value = ([], 0)
    return service, repositories


def _content(execution_id: str = "fill-1", quantity: str = "2") -> bytes:
    record = {
        "execution_id": execution_id,
        "executed_at": "2026-03-14T10:31:07Z",
        "symbol": "AAPL",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": quantity,
        "price": "100",
        "currency": "USD",
    }
    return json.dumps({"schema_version": "1", "executions": [record]}).encode()


async def test_import_trades_generic_without_account_id_rejects_before_any_repository_call(mock_session: AsyncMock) -> None:
    service, repositories = _service(mock_session, AccountType.GENERIC)
    with pytest.raises(ImportNotAllowedError, match="account_id"):
        await service.import_trades(_content(), "fills.json", BrokerType.GENERIC, uuid4(), None, uuid4())
    for repository in repositories.values():
        assert repository.mock_calls == []
    mock_session.execute.assert_not_awaited()


def _account(account_type: AccountType, *, account_id: UUID | None = None, user_id: UUID | None = None, portfolio_id: UUID | None = None) -> Account:
    return Account(
        id=account_id or uuid4(),
        user_id=user_id or uuid4(),
        portfolio_id=portfolio_id or uuid4(),
        name="File-only account",
        type=account_type,
        currency="USD",
        is_active=True,
        created_at=datetime(2026, 3, 14, tzinfo=UTC),
        properties=[],
    )


def _account_service(session: AsyncMock, account: Account, *, has_history: bool) -> tuple[AccountService, AsyncMock]:
    service = AccountService(session)
    repository = AsyncMock()
    repository.get_by_id.return_value = account
    repository.update.return_value = account
    repository.has_trading_history.return_value = has_history
    service._repo = cast(AccountRepository, repository)
    for name in ("_trade_repo", "_exec_repo", "_raw_repo"):
        setattr(service, name, AsyncMock())
    return service, repository


_GENERIC_TRANSITIONS = [
    (AccountType.GENERIC, AccountType.IBKR),
    (AccountType.GENERIC, AccountType.SCHWAB),
    (AccountType.GENERIC, AccountType.MANUAL),
    (AccountType.IBKR, AccountType.GENERIC),
    (AccountType.SCHWAB, AccountType.GENERIC),
    (AccountType.MANUAL, AccountType.GENERIC),
]


@pytest.mark.parametrize(("source_type", "target_type"), _GENERIC_TRANSITIONS)
async def test_update_account_namespace_transition_with_history_rejects_under_account_lock(
    mock_session: AsyncMock, source_type: AccountType, target_type: AccountType
) -> None:
    account = _account(source_type)
    service, repository = _account_service(mock_session, account, has_history=True)
    with pytest.raises(AccountValidationError, match="history"):
        await service.update_account(account.id, AccountUpdate(type=target_type), account.user_id, account.portfolio_id)
    repository.get_by_id.assert_awaited_once_with(account.id, account.user_id, account.portfolio_id, for_update=True)
    repository.has_trading_history.assert_awaited_once_with(account.id)
    assert repository.mock_calls[:2] == [
        call.get_by_id(account.id, account.user_id, account.portfolio_id, for_update=True),
        call.has_trading_history(account.id),
    ]
    repository.update.assert_not_awaited()
    repository.upsert_property.assert_not_awaited()
    assert account.type == source_type
    mock_session.flush.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


@pytest.mark.parametrize(("source_type", "target_type"), _GENERIC_TRANSITIONS)
async def test_update_account_empty_namespace_transition_flushes_without_committing_the_lock(
    mock_session: AsyncMock, source_type: AccountType, target_type: AccountType
) -> None:
    account = _account(source_type)
    updated = _account(target_type, account_id=account.id, user_id=account.user_id, portfolio_id=account.portfolio_id)
    service, repository = _account_service(mock_session, account, has_history=False)
    repository.get_by_id.side_effect = [account, updated]
    repository.update.return_value = updated
    result = await service.update_account(account.id, AccountUpdate(type=target_type), account.user_id, account.portfolio_id)
    assert result.type == target_type
    repository.has_trading_history.assert_awaited_once_with(account.id)
    assert repository.get_by_id.await_args_list == [
        call(account.id, account.user_id, account.portfolio_id, for_update=True),
        call(account.id, account.user_id),
    ]
    mock_session.flush.assert_awaited_once()
    mock_session.commit.assert_not_awaited()


@pytest.mark.parametrize("account_type", [AccountType.GENERIC, AccountType.IBKR])
async def test_update_account_unchanged_type_with_history_does_not_run_transition_guard(mock_session: AsyncMock, account_type: AccountType) -> None:
    account = _account(account_type)
    service, repository = _account_service(mock_session, account, has_history=True)
    await service.update_account(account.id, AccountUpdate(type=account_type), account.user_id, account.portfolio_id)
    repository.has_trading_history.assert_not_awaited()
    repository.update.assert_awaited_once()
    assert repository.get_by_id.await_args_list[0] == call(account.id, account.user_id, account.portfolio_id, for_update=True)
    mock_session.flush.assert_awaited_once()
    mock_session.commit.assert_not_awaited()


async def test_update_account_credential_only_patch_locks_generic_account_before_rejecting_properties(mock_session: AsyncMock) -> None:
    account = _account(AccountType.GENERIC)
    service, repository = _account_service(mock_session, account, has_history=False)
    with pytest.raises(AccountValidationError, match="credentials"):
        await service.update_account(account.id, AccountUpdate(properties={"api_secret": "never-store"}), account.user_id, account.portfolio_id)
    repository.get_by_id.assert_awaited_once_with(account.id, account.user_id, account.portfolio_id, for_update=True)
    repository.has_trading_history.assert_not_awaited()
    repository.upsert_property.assert_not_awaited()
    repository.update.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


async def test_fresh_start_import_keeps_account_lock_across_deletions_and_delegated_import(mock_session: AsyncMock) -> None:
    service, repositories = _service(mock_session, AccountType.GENERIC)
    repositories["_exec_repo"].delete_all_for_account.return_value = 2
    repositories["_raw_repo"].delete_by_user_account.return_value = 2
    repositories["_trade_repo"].delete_by_user_account.return_value = 1
    result = ImportResult(broker="generic", filename="fills.json", trades_parsed=1, trades_created=1, trades_skipped=0)
    account_id, user_id, portfolio_id = uuid4(), uuid4(), uuid4()
    with patch.object(service, "import_trades", new=AsyncMock(return_value=result)) as delegated:
        fresh = await service.fresh_start_import(_content(), "fills.json", BrokerType.GENERIC, portfolio_id, account_id, user_id)
    repositories["_account_repo"].get_by_id.assert_awaited_once_with(account_id, user_id, portfolio_id, for_update=True)
    delegated.assert_awaited_once_with(
        content=_content(),
        filename="fills.json",
        broker=BrokerType.GENERIC,
        portfolio_id=portfolio_id,
        account_id=account_id,
        user_id=user_id,
        overwrite_manually_edited=True,
    )
    assert fresh.executions_deleted == 2
    assert fresh.trades_deleted == 1
    mock_session.commit.assert_not_awaited()


@pytest.mark.parametrize("account_type", [AccountType.IBKR, AccountType.SCHWAB, AccountType.MANUAL])
async def test_import_trades_generic_with_non_generic_account_rejects_before_parsing_or_writes(mock_session: AsyncMock, account_type: AccountType) -> None:
    service, repositories = _service(mock_session, account_type)
    account_id, user_id, portfolio_id = uuid4(), uuid4(), uuid4()
    with pytest.raises(ImportNotAllowedError, match="only.*generic accounts"):
        await service.import_trades(b"deliberately invalid", "fills.csv", BrokerType.GENERIC, portfolio_id, account_id, user_id)
    repositories["_account_repo"].get_by_id.assert_awaited_once_with(account_id, user_id, portfolio_id, for_update=True)
    for name, repository in repositories.items():
        if name != "_account_repo":
            assert repository.mock_calls == []
    mock_session.execute.assert_not_awaited()


@pytest.mark.parametrize("broker", [BrokerType.IBKR, BrokerType.SCHWAB])
async def test_import_trades_native_broker_with_generic_account_rejects_before_parsing_or_writes(mock_session: AsyncMock, broker: BrokerType) -> None:
    service, repositories = _service(mock_session, AccountType.GENERIC)
    with pytest.raises(ImportNotAllowedError, match="generic CSV/JSON"):
        await service.import_trades(b"deliberately invalid", "fills.json", broker, uuid4(), uuid4(), uuid4())
    for name, repository in repositories.items():
        if name != "_account_repo":
            assert repository.mock_calls == []
    mock_session.execute.assert_not_awaited()


async def test_import_trades_generic_unknown_account_raises_not_found_before_other_repository_calls(mock_session: AsyncMock) -> None:
    service, repositories = _service(mock_session, None)
    with pytest.raises(AccountNotFoundError):
        await service.import_trades(_content(), "fills.json", BrokerType.GENERIC, uuid4(), uuid4(), uuid4())
    for name, repository in repositories.items():
        if name != "_account_repo":
            assert repository.mock_calls == []
    mock_session.execute.assert_not_awaited()


async def test_import_trades_generic_invalid_document_never_calls_any_persistence_method(mock_session: AsyncMock) -> None:
    service, repositories = _service(mock_session, AccountType.GENERIC)
    with pytest.raises(GenericImportValidationError):
        await service.import_trades(_content(quantity="0"), "fills.json", BrokerType.GENERIC, uuid4(), uuid4(), uuid4())
    assert repositories["_raw_repo"].mock_calls == []
    assert repositories["_logical_svc"].mock_calls == []
    assert repositories["_exec_repo"].mock_calls == []
    repositories["_ca_repo"].create.assert_not_awaited()
    repositories["_trade_repo"].create.assert_not_awaited()
    repositories["_trade_repo"].delete_by_user_account.assert_not_awaited()
    mock_session.add.assert_not_called()
    mock_session.flush.assert_not_awaited()
    mock_session.execute.assert_not_awaited()


@pytest.mark.parametrize("execution_id", ["fill-1", "x" * 100])
async def test_import_trades_generic_stamps_account_namespace_before_user_namespace(mock_session: AsyncMock, execution_id: str) -> None:
    service, repositories = _service(mock_session, AccountType.GENERIC)
    account_id, user_id, portfolio_id = uuid4(), uuid4(), uuid4()
    result = await service.import_trades(_content(execution_id), "fills.json", BrokerType.GENERIC, portfolio_id, account_id, user_id)
    raw_repo = repositories["_raw_repo"]
    raw_repo.bulk_upsert.assert_awaited_once()
    records: list[BrokerRawTransactionCreate] = raw_repo.bulk_upsert.await_args.args[0]
    assert len(records) == 1
    assert records[0].id == f"{user_id}:generic:{account_id}:{execution_id}"
    assert len(records[0].id) <= 200
    assert records[0].user_id == user_id
    assert records[0].account_id == account_id
    assert isinstance(records[0].account_id, UUID)
    repositories["_account_repo"].get_by_id.assert_awaited_once_with(account_id, user_id, portfolio_id, for_update=True)
    for method in ("list_open_lots_by_user", "list_orphaned_lots_by_user", "list_manually_edited_keys"):
        getattr(repositories["_trade_repo"], method).assert_awaited_once_with(user_id, portfolio_id, account_id=account_id)
    assert result.raw_transactions_created == 0
    assert result.trades_created == 0
    mock_session.execute.assert_not_awaited()
