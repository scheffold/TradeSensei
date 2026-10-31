## Why

All broker data already flows through one pipeline (`parse_raw()` → `BrokerRawTransactionCreate` → `ImportService.import_trades()`), but the intermediate model is not yet a complete, explicit contract. Parts of the pipeline semantics live in IBKR-named `raw_data` keys (`brokerageOrderID`, `netCash`, `openCloseIndicator`). Closing legs are inferred from `pnl is not None`, which forces Schwab and generic imports to emit a synthetic `pnl=0`. `ImportService` also branches on concrete broker types. Because of this, adding a broker today means understanding pipeline internals instead of only mapping export fields. The goal is to make "add broker XY" a pure mapping task that is documented, contract-tested and repeatable by a dedicated implementation agent. This applies to both file upload and API pull.

## What Changes

- **Explicit intermediate model**: `BrokerRawTransactionCreate` (and the `broker_raw_transactions` table) gain first-class fields:
  - `order_id`: broker order grouping key.
  - `net_cash`: signed order cash.
  - `position_effect`: `OPEN | CLOSE | FLIP | UNKNOWN`.

  `LogicalExecution` carries `position_effect`. A data migration backfills all three from existing columns and `raw_data`, so existing trade projections stay unchanged.
- **Closing semantics decoupled from P&L**: the FIFO aggregator, the execution→trade conversion and the split warning decide opening vs. closing from `position_effect`, not from `pnl` presence. Parsers keep emitting today's `pnl` values in this change, so P&L output is unchanged. Removing the synthetic zero is a follow-up.
- **No pipeline semantics in `raw_data`**: combo metadata and flip detection read the new columns. `raw_data` becomes purely informational for all brokers. The generic parser stops writing IBKR-named top-level keys.
- **Broker adapter registry**: one declarative registration per broker replaces the scattered maps in `factory.py` and `api/v1/imports.py`. Each registration covers:
  - broker type and account type
  - allowed file extensions
  - mapper (parser)
  - capability flags, for example account-scoped raw IDs, history-based order-marker validation, strict persistence errors, and pending-protected-close rebuild

  `ImportService` reacts to capabilities instead of `if broker == ...`.
- **Mapper contract**: `parse_raw()` returns a `ParseResult` (records, corporate actions, warnings) instead of a tuple plus mutable `parser.warnings`. Shared normalisation helpers (UTC timestamps, decimals, OCC parsing, asset-class mapping) are consolidated.
- **Connector contract**: API sync is formalised as `BrokerConnector.fetch(account, window) → FetchedPayload(content, filename, bound_account_number)`. Authentication stays broker-specific inside the connector. `BrokerService` runs one generic sync flow (fetch → `import_trades` → `BrokerSyncResult`). This includes the Schwab account-number auto-binding and its audit event, which become generic.
- **Conformance test kit**: a parametrised contract test suite runs against every registered adapter and its fixtures. It checks invariants (ID prefix and uniqueness, UTC, positive quantity, `position_effect` set, determinism, purity, JSON-safe `raw_data`). A new broker is covered automatically when it registers fixtures.
- **BREAKING (internal)**: the legacy `BrokerParser.parse()` path, the `ImportService` legacy fallback, IBKR's TradeCreate-producing helpers and the never-overridden `enrich_pnl_eur` hook are removed. There is no API or user-visible change.
- **Documentation and tooling**:
  - `docs/broker-integration-guide.md`: architecture, field-by-field reference of the intermediate model, mapping recipe, auth patterns, checklist.
  - Subagent `broker-integration-engineer`: implements mapper, connector, fixtures and tests for a new broker.
  - Skill `add-broker`: collects the required inputs (sample exports, field docs, delivery modes, auth mechanism, semantics questionnaire), then invokes the agent.
  - Updates to AGENTS.md and the existing format docs.

## Capabilities

### New Capabilities
- `broker-adapter-contract`: The broker adapter registry, mapper and connector contracts, the semantics of the intermediate execution model, capability-driven import behaviour, the conformance test kit, and the broker integration guide/tooling.

### Modified Capabilities
- `raw-transaction-layer`: Raw transactions and logical executions carry first-class `order_id`, `net_cash` and `position_effect`; pipeline semantics are no longer read from `raw_data`.
- `pnl-calculation`: The closing-leg decision moves from `pnl` presence to `position_effect`; no component (including the aggregator) infers position effect from P&L presence.
- `generic-execution-import`: Generic records map `order_id`/derived cash/`open_close` onto the first-class fields instead of top-level `raw_data` keys; the parser implements the mapper contract.
- `trade-import`: Remote-fetch paths are defined by the connector contract and a single generic sync flow.

## Impact

- **Backend**:
  - Parsers and registry: `app/utils/parsers/` (`base.py`, `factory.py` → registry, `ibkr_xml.py`, `schwab_json.py`, `generic.py`, `aggregator.py`, new shared helpers module).
  - Schemas and models: `app/schemas/broker_raw_transaction.py`, `app/schemas/generic_import.py`, `app/models/broker_raw_transaction.py`, `app/models/logical_execution.py`.
  - Repositories: `app/repositories/broker_raw_transaction_repository.py`.
  - Services: `app/services/import_service.py`, `logical_execution_service.py`, `broker_service.py`, `trade_service.py` (manual close legs set `position_effect`).
  - API and constants: `app/api/v1/imports.py`, `app/core/constants.py`.
- **Database**: one Alembic migration that adds three columns to `broker_raw_transactions` and `position_effect` to `logical_executions`, backfills them, and creates a PostgreSQL enum `positioneffect`.
- **Tests**:
  - New conformance kit under `backend/tests/contract/`.
  - Golden projection snapshots for all existing fixtures, taken before the refactor and asserted after it.
  - Migration of tests that call the legacy `parse()`.
- **Tooling and docs**:
  - New: `.opencode/agents/broker-integration-engineer.md`, `.opencode/skills/add-broker/SKILL.md`, `docs/broker-integration-guide.md`.
  - Updated: `AGENTS.md`, `docs/generic-import-format.md`, `CHANGELOG.md`.
- **Unchanged**: public API, generic v1 file format, frontend, trade/P&L results of existing data.
- **Out of scope (follow-ups)**:
  - Schwab combo support (order and instrument IDs from Schwab).
  - Removing the synthetic `pnl=0`.
  - Merging `open_close` into `position_effect`.
  - Renaming `conid` to `instrument_id`.
  - Registry-driven broker list for the frontend.
