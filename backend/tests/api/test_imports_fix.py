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

import uuid
from unittest.mock import AsyncMock

import pytest

from app.api.v1.imports import _validate_account_import
from app.core.exceptions import AccountNotFoundError, ImportNotAllowedError
from app.models.account import Account, AccountType
from app.utils.parsers.factory import BrokerType


@pytest.mark.asyncio
async def test_validate_account_import_success():
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    user_id = uuid.uuid4()
    
    mock_account = Account(id=account_id, type=AccountType.IBKR)
    mock_service = AsyncMock()
    mock_service.get_account.return_value = mock_account
    broker = await _validate_account_import(account_id, portfolio_id, user_id, "xml", mock_service)
    assert broker == BrokerType.IBKR
    mock_service.get_account.assert_awaited_once_with(account_id, user_id, portfolio_id)

    # Test not found
    mock_service.get_account.side_effect = AccountNotFoundError("Not found")
    with pytest.raises(AccountNotFoundError):
        await _validate_account_import(account_id, portfolio_id, user_id, "xml", mock_service)
    mock_service.get_account.side_effect = None

    # Test manual
    mock_account.type = AccountType.MANUAL
    with pytest.raises(ImportNotAllowedError):
        await _validate_account_import(account_id, portfolio_id, user_id, "xml", mock_service)

    # Test bad extension
    mock_account.type = AccountType.IBKR
    with pytest.raises(ImportNotAllowedError):
        await _validate_account_import(account_id, portfolio_id, user_id, "csv", mock_service)

    # Test schwab csv exception
    mock_account.type = AccountType.SCHWAB
    with pytest.raises(ImportNotAllowedError) as exc_info:
        await _validate_account_import(account_id, portfolio_id, user_id, "csv", mock_service)
    assert "Schwab accounts require JSON format" in str(exc_info.value)
