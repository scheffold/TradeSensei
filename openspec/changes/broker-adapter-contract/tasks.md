## 1. Safety net (before any production code change)

- [ ] 1.1 Add `backend/tests/integration/test_import_golden_snapshots.py`. It imports every existing fixture into a fresh account:
  - IBKR combined, IBKR combo positions, IBKR split fixtures, Flex samples
  - Schwab aggregation and short round-trip
  - all generic parity fixtures

  For each import it serialises the raw transactions (excluding ID and timestamp columns), logical executions and trades to deterministic JSON under `tests/test_data/golden/`
- [ ] 1.2 Generate and commit the golden files from the current `main` code. The test asserts equality and supports an explicit allow-list of new columns
- [ ] 1.3 Add a read-only diagnostic SQL script `tools/diagnostics/position_effect_report.sql`. It counts raw and logical rows per broker by `(pnl is null, open_close, mixed indicator)`, including rows where `open_close='C'` and `pnl IS NULL`. Document how to run it in the script header

## 2. Data model and migration

- [ ] 2.1 Add the `PositionEffect` StrEnum (`OPEN`, `CLOSE`, `FLIP`, `UNKNOWN`) and the shared pure helper `legacy_position_effect(pnl, open_close, is_mixed)` (design D1) in the parsers' common module, with unit tests for every branch
- [ ] 2.2 Add `position_effect`, `order_id` and `net_cash` to the `BrokerRawTransaction` model, and `position_effect` to the `LogicalExecution` model. Update `BrokerRawTransactionCreate`/`Read` and the execution read schemas. `position_effect` is required on Create
- [ ] 2.3 Create an Alembic revision via the `db-migration` skill:
  - enum, nullable columns, batched backfill per D6 (top-level `raw_data` keys only), `NOT NULL`, partial index `(user_id, account_id, order_id)`
  - a lossless downgrade
- [ ] 2.4 Migration test: seed rows covering every D1 branch, including generic rows with nested `source_record` keys and logical rows with user-edited `pnl`. Run upgrade, assert the backfilled values, then run downgrade/upgrade round-trip
- [ ] 2.5 `LogicalExecutionService.create_from_raw` copies `position_effect` and sets `is_mixed_execution = (position_effect == FLIP)`. Stop reading `raw_data`. Remove `BrokerRawTransactionCreate.is_mixed_execution`
- [ ] 2.6 Set `position_effect=UNKNOWN` for all manual execution creation paths: `TradeService` partial/full close, the leg editor, and `ExecutionRepository.create` callers. Make split and merge inherit it from the source execution. Add tests per path

## 3. Aggregator switch to `position_effect`

- [ ] 3.1 Add `position_effect` to the execution-like `TradeCreate` rows and to `iter_logical_executions_to_trade_creates`
- [ ] 3.2 Replace every closing decision in `aggregator.py` that is based on `pnl is not None` (closed_at derivation, `is_closing_leg`, split warning, orphan remainder) with a single `_is_closing(position_effect)` predicate
- [ ] 3.3 Repository `get_combo_order_meta_by_raw_ids` and `get_generic_order_markers` read the `order_id`/`net_cash` columns. Keep the return shape unchanged
- [ ] 3.4 IBKR mapper sets `order_id`, `net_cash` and `position_effect` via `legacy_position_effect`. The Schwab mapper sets `position_effect` from `positionEffect`. `GenericExecutionV1.to_raw()` sets `order_id`, `net_cash` and `position_effect` per the modified generic spec and writes only `source_record` into `raw_data`. Delete the `GENERIC_PIPELINE_*_KEY` constants
- [ ] 3.5 Update existing unit tests that assert on `raw_data` pipeline keys or on `pnl`-based closing (aggregator, combo builder, generic parser, Schwab, IBKR). Golden snapshots (1.1) pass with only the allow-listed new columns differing

## 4. Adapter registry and capability-driven ImportService

- [ ] 4.1 Create `app/utils/parsers/registry.py` with `BrokerType`, `BrokerCapabilities`, `BrokerAdapter`, `get_adapter()`, `adapter_for_account_type()` and `all_adapters()`. Register IBKR, Schwab and generic with today's extensions, account types, flags and contract fixtures
- [ ] 4.2 Replace `factory.get_parser`, `imports._BROKER_ALLOWED_EXTENSIONS` and `imports._ACCOUNT_TYPE_BROKER` with registry lookups. Update all imports of `BrokerType` and delete `factory.py`
- [ ] 4.3 Replace each `BrokerType` comparison in `ImportService` with its capability:
  - `exclusive_account_type` (L99–105)
  - `validate_order_marker_history` (L439)
  - `raw_id_scope` (L471)
  - `rebuild_on_pending_protected_close` (L521)
  - `strict_persistence_errors` (L774)
- [ ] 4.4 Verify that the existing tests for generic account isolation, order-marker history, Schwab pending-protected-close and the generic persistence error still pass unchanged

## 5. Mapper contract

- [ ] 5.1 Introduce `ParseResult` and change `BrokerParser.parse_raw()` to return it. Remove mutable `self.warnings` from all parsers. Make `ImportService` consume `ParseResult.warnings`
- [ ] 5.2 Add the optional pure hook `select_account(content, configured_account_number) -> AccountSelection`. Move the Schwab account selection out of `ImportService` (L373) into `SchwabJSONParser.select_account`
- [ ] 5.3 Extract a shared `bind_account_number(account, number, provider)` service helper that emits `account.account_number_bound` with safe metadata (`reason="single_account_auto_bind"`, `provider`). Use it from `ImportService` and `BrokerService`, and update the audit tests
- [ ] 5.4 Create `app/utils/parsers/common.py` (UTC datetime parsing, `safe_decimal`, asset-class helpers). Replace IBKR `_extract_occ_underlying` with `occ.extract_occ_underlying`. Remove duplicated helpers from the mappers

## 6. Connector contract and unified sync

- [ ] 6.1 Create `app/services/broker_connectors/` with `BrokerConnector`, `FetchedPayload` and `SyncWindow` (`base.py`), `SchwabConnector` (OAuth token refresh, account resolution, transactions fetch, wrapped payload) and `IbkrFlexConnector` (Flex token and query ID from account properties, `FlexImportService.fetch_flex_report`)
- [ ] 6.2 Replace `BrokerService._sync_schwab`/`_sync_ibkr` with one `sync_account` flow (fetch → `bind_account_number` → `import_trades` → `BrokerSyncResult`). Keep the per-connector exception translation
- [ ] 6.3 Make `/imports/schwab-json` and `/imports/flex` use registry lookups instead of hard-coded `BrokerType`/`AccountType` checks. Their behaviour and status codes stay unchanged
- [ ] 6.4 Update `test_broker_service.py` for both connectors. Add a test proving that upload and sync of the same payload produce identical raw transactions

## 7. Legacy removal

- [ ] 7.1 Remove `BrokerParser.parse()`, `enrich_pnl_eur`, the `ImportService` legacy fallback branch, IBKR `parse`/`_parse_trade_element`/`_parse_execution_element`, and the `parse()` stubs in Schwab and generic
- [ ] 7.2 Migrate tests that call `parser.parse(...)` (`test_import.py`, `unit/test_ibkr_xml.py`, `unit/test_schwab_json.py`, `unit/test_generic_parser.py`) to `parse_raw()`, or to the projection via `ImportService`, without losing assertions

## 8. Conformance kit

- [ ] 8.1 Create `backend/tests/contract/test_broker_adapter_contract.py`, parametrised over `all_adapters()` × `contract_fixtures`. It asserts:
  - ID prefix and in-file uniqueness, UTC, `quantity > 0`
  - non-null `position_effect`, determinism (map twice and compare)
  - JSON-serialisable `raw_data`, `order_id ⇒ conid`, no `OPEN` row with `pnl`
  - mapper purity: no session or network, checked via monkeypatched sockets
  - for the three legacy adapters: `position_effect == legacy_position_effect(...)`
- [ ] 8.2 Fail with a clear message when an adapter registers no contract fixtures
- [ ] 8.3 Static checks: `import_service.py` and `broker_service.py` contain no `BrokerType.<MEMBER>` references, and every `BrokerRawTransactionCreate` field appears in the field-reference table of `docs/broker-integration-guide.md`

## 9. Documentation

- [ ] 9.1 Write `docs/broker-integration-guide.md` per design D8:
  - data flow and field reference
  - `position_effect` decision table and the `open_close` vs `position_effect` explanation
  - mapper and connector contracts, capability catalogue
  - auth patterns (Schwab OAuth, IBKR Flex, file-only)
  - fixture rules, frontend touch-point list, definition-of-done checklist
- [ ] 9.2 Update `docs/generic-import-format.md` (first-class order fields, `position_effect` mapping) and `docs/api-usage.md` if any examples change
- [ ] 9.3 Rewrite AGENTS.md § "Broker Ingestion Paths" around adapter, mapper and connector, and link the guide. Add the new agent and skill rows to the tables in §8. Update the anti-pattern in `.opencode/agents/backend-engineer.md` to reference connectors and the registry

## 10. Broker integration tooling

- [ ] 10.1 Create `.opencode/agents/broker-integration-engineer.md`:
  - **Scope:** adapter registration, mapper, optional connector, `AccountType` value and migration, fixtures, golden snapshot, mapper tests
  - **Hard rules:**
    - no changes to the aggregator, `ImportService` internals or other mappers
    - no `raw_data` semantics and no broker branches
    - stop and report contract gaps
  - **Verification:** contract kit, `lint-check`, `unit-test`
  - **Handoff:** frontend touch points to `frontend-engineer`
- [ ] 10.2 Create `.opencode/skills/add-broker/SKILL.md`:
  - intake checklist and semantics questionnaire (design D8)
  - escape hatch to the generic format when stable execution IDs are missing
  - invocation of `broker-integration-engineer`, then `code-reviewer`, then `changelog-writer`
- [ ] 10.3 Dry-run the skill against an existing broker (Schwab file import) in a scratch branch. Verify that the intake questions are sufficient to reproduce the registration and mapper skeleton, then refine the skill and guide wording

## 11. Verification and wrap-up

- [ ] 11.1 Run the full backend suite, `mypy --strict` and ruff. Golden snapshots, contract kit and parity tests are all green
- [ ] 11.2 Run the migration against a copy of a real database. The diagnostic script reports zero mismatches between the backfilled `position_effect` and the legacy rule. A projection rebuild of every account yields unchanged trades
- [ ] 11.3 Run `code-reviewer` over the change (architecture, audit metadata safety, no broker branches)
- [ ] 11.4 Add a CHANGELOG entry via `changelog-writer` (internal refactor, new docs and tooling)
- [ ] 11.5 Create follow-up OpenSpec proposals or notes:
  - Schwab order and instrument IDs (combos)
  - drop synthetic `pnl=0`
  - merge `open_close` into `position_effect`
  - unconditional pending-protected-close rebuild
  - registry-driven frontend broker list
