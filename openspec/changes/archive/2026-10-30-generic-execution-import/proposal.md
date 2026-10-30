## Why

Only IBKR (Flex Query XML) and Schwab (JSON) can be imported today. Users of any other broker — or users who want to record executions by hand in a spreadsheet — have no way to get trades into Trade Sensei short of entering them one by one in the UI. A single, documented, broker-neutral execution format lets any user convert their broker export (or author a file manually) and feed it through the exact same import pipeline as the native brokers.

## What Changes

- Define a versioned, broker-neutral **generic execution format (v1)**: one record per execution (fill), not per trade. Accepted encodings: **CSV** and **JSON**. XML is explicitly out of scope.
- `execution_id` is **mandatory**. It is the idempotency key that makes re-imports safe; files without it are rejected.
- Add `GenericParser` (implements `parse_raw()` only) that maps v1 records 1:1 onto `BrokerRawTransactionCreate` and hands them to `ImportService.import_trades()` — no parallel ingestion path.
- Add `BrokerType.GENERIC` and a new account type `GENERIC` ("Other broker"). Generic files are accepted **only** by generic accounts; idempotency keys are scoped per account so two generic sources can never collide.
- All-or-nothing validation: every row error is collected (line number + field) and the whole file is rejected if any row is invalid.
- Publish a machine-readable **JSON Schema** (generated from the Pydantic model) and a **CSV template**, both downloadable via the API and linked from the import UI.
- Documentation: a dedicated format README (`docs/generic-import-format.md`), a new "Importing from other brokers" section in the in-app help (`help-content.ts`), and the import form shows the format hint plus template links for generic accounts.
- Parity test suite: the existing IBKR and Schwab fixtures are converted into generic CSV and JSON fixtures; importing the generic version MUST yield the same trades as importing the native file.

## Capabilities

### New Capabilities
- `generic-execution-import`: The v1 generic execution format (fields, encodings, validation rules), its parser, idempotency semantics, schema/template downloads, documentation, and the native-vs-generic parity guarantee.

### Modified Capabilities
- `account-management`: The accepted account type enumeration is extended with `GENERIC`; generic accounts accept generic-format file uploads only.

## Impact

- **Backend**: `app/utils/parsers/` (new `generic.py`, `factory.py`), `app/models/account.py` (`AccountType.GENERIC`), `app/api/v1/imports.py` (allowed extensions, account→broker map, schema/template endpoints), `app/services/import_service.py` (account-scoped raw IDs for generic), new Pydantic schema module for the v1 record.
- **Database**: Alembic migration adding `generic` to the PostgreSQL enum `accounttype`. No table changes.
- **Frontend**: account create/edit modals and `AccountForm` (new type option), `ImportForm` (accept `.csv,.json`, format hint, template links), `AccountList` badge, `help-content.ts`; regenerated OpenAPI client.
- **Docs**: `docs/generic-import-format.md`, `docs/api-usage.md`, `docs/HELP_GLOSSARY.md` export, `CHANGELOG.md`.
- **Tests**: new generic fixtures under `tests/test_data/generic/`, a fixture conversion tool, parser unit tests, API tests, and native-vs-generic parity integration tests.
- **Unchanged**: IBKR and Schwab parsers and their import behaviour.
