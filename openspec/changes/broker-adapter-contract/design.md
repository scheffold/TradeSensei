## Context

The import pipeline already has one shape for all brokers:

```
upload (bytes) ─┐
API pull (bytes)┴→ BrokerParser.parse_raw() → [BrokerRawTransactionCreate] → ImportService.import_trades()
                     → broker_raw_transactions → logical_executions → combo builder + FIFO → trades
```

`IBKRXMLParser`, `SchwabJSONParser` and `GenericParser` already produce the broker-neutral DTO, and the generic v1 format is a user-facing serialisation of it. The contract is still incomplete, and those gaps are what make a new broker more than a mapping job. The following was verified in code:

| Gap | Where |
|---|---|
| Combo grouping and cash are read from IBKR attribute names in `raw_data` (`brokerageOrderID`, `netCash`) | `broker_raw_transaction_repository.get_combo_order_meta_by_raw_ids`, `import_service` ~L1974 |
| Flip detection reads `raw_data["openCloseIndicator"]` | `logical_execution_service` L100, `BrokerRawTransactionCreate.is_mixed_execution` |
| A closing leg is `pnl is not None`, which forces synthetic `pnl=0` in Schwab, generic and IBKR breakeven closes | `aggregator.py` L492, L687, L748, L776, L835 |
| `ImportService` branches on `BrokerType` values | L99–105 (account/type guard), L373 (Schwab account selection and binding), L439 (generic order-marker history), L471 (generic account-scoped ID), L521 (Schwab-only pending-protected-close rebuild), L774 (generic strict persistence errors) |
| Broker metadata is spread over three maps | `factory.get_parser`, `imports._BROKER_ALLOWED_EXTENSIONS`, `imports._ACCOUNT_TYPE_BROKER` |
| Mapper warnings live in mutable instance state (`parser.warnings`), and `parse_raw` returns a bare tuple | all parsers |
| A legacy path is still present: abstract `parse()`, the `ImportService` fallback (L672), IBKR `_parse_trade_element`/`_parse_execution_element`, and `enrich_pnl_eur` (never overridden). All registered parsers implement `parse_raw`, so the fallback is unreachable in production | `base.py`, `ibkr_xml.py`, `import_service.py` |
| The two sync paths duplicate fetch → import → result mapping, and the Schwab account-number binding and audit are implemented twice | `broker_service._sync_schwab`, `_sync_ibkr`, `import_service` L373 |

The user decided:
- Use an explicit position-effect field instead of the P&L marker.
- Keep Schwab combos as a follow-up.
- Ship the tooling as subagent, skill and guide.

## Goals / Non-Goals

**Goals:**
- `BrokerRawTransactionCreate` is a complete, documented contract. Every field the pipeline interprets is first-class.
- Adding a broker means: register an adapter, write a pure mapper, optionally write a connector, and add fixtures. The conformance kit, the pipeline and the protections apply automatically.
- `ImportService` and `BrokerService` contain no concrete broker comparisons.
- **The change is behaviour-neutral.** Every existing fixture produces byte-identical trade projections before and after, and existing data is backfilled so that rebuilds are no-ops.
- Documentation, a subagent and a skill make new broker integrations repeatable.

**Non-Goals:**
- Schwab combo support. Schwab has no order or instrument IDs mapped today, so this is a follow-up.
- Removing synthetic `pnl=0` emission from parsers. This is a follow-up and changes orphan and combo P&L inputs.
- Merging `open_close` into `position_effect`. The combo builder keeps reading `open_close`, and IBKR `<Execution>` rows can carry `open_close='C'` with `pnl=None`, which today are not closing.
- Renaming `conid` → `instrument_id` or `broker_conid`.
- A registry-driven broker list for the frontend. Account types stay hard-coded there in this change.
- A user-configurable column mapping UI.
- Changing the `/schwab-json` and `/flex` endpoints. They already delegate to `import_trades()`, and only their broker lookups move to the registry.

## Decisions

### D1 — `position_effect` enum with a behaviour-preserving derivation
New PostgreSQL enum `positioneffect` with values `OPEN | CLOSE | FLIP | UNKNOWN`, a non-null column on `broker_raw_transactions` and `logical_executions`, and a required field on `BrokerRawTransactionCreate`.

The aggregator's `is_closing_leg` becomes `position_effect in {CLOSE, FLIP}`. The same predicate replaces the four other `pnl is not None` checks in `aggregator.py` (closed_at in the execution→trade conversion, the split warning, and the orphan-remainder branch). `is_mixed_execution` is set to `position_effect == FLIP`. The execution-like `TradeCreate` rows carry `position_effect`.

To guarantee parity, every existing mapper and the migration use one shared helper, `legacy_position_effect(pnl, open_close, is_mixed)`:

```
pnl is not None and is_mixed     → FLIP
pnl is not None                  → CLOSE
pnl is None and open_close=="O"  → OPEN
otherwise                        → UNKNOWN
```

This reproduces today's closing decision exactly. One deliberate consequence: an IBKR mixed row whose `fifoPnlRealized` is 0 maps to `UNKNOWN` rather than `FLIP`. Today it is not a closing leg, and its `is_mixed` flag has no effect outside closing legs, so behaviour is unchanged.

Native mappers compute the effect in their own terms. Schwab: `CLOSING→CLOSE`, `OPENING→OPEN`. Generic: per the modified generic spec. The conformance kit asserts, for the three legacy adapters only, that this equals `legacy_position_effect(...)`.

*Alternative:* reuse `open_close` plus `is_mixed_execution` as the signal. Rejected: IBKR `<Execution>` rows have `open_close='C'` with `pnl=None` and are not closing today, so switching would silently change projections.

*Alternative:* three values without `UNKNOWN`. Rejected: manual legs and IBKR rows without an indicator would be mislabelled `OPEN`.

### D2 — First-class `order_id` and `net_cash`
`order_id` is `String(100)` and `net_cash` is `Numeric(18, 8)`, both nullable, on `broker_raw_transactions` only. Logical executions keep reaching them via `source_id`, as today.

- The repository method `get_combo_order_meta_by_raw_ids` reads the columns and returns the same dict shape, so the combo builder is untouched.
- `get_generic_order_markers` filters on the `order_id` column.
- The IBKR mapper fills both from the `brokerageOrderID` and `netCash` attributes.
- `GenericExecutionV1.to_raw()` fills them directly and no longer writes top-level `raw_data` keys.
- The `GENERIC_PIPELINE_*_KEY` constants are deleted.
- Index: `(user_id, account_id, order_id)` partial index where `order_id IS NOT NULL`, which serves the generic history check.

*Alternative:* keep `raw_data` keys and document them. Rejected: this makes IBKR's attribute naming part of every broker's contract and keeps the trust-boundary special case for generic.

### D3 — Adapter registry and capability flags
`app/utils/parsers/registry.py` (pure, no DB) defines:

```python
@dataclass(frozen=True)
class BrokerCapabilities:
    raw_id_scope: Literal["user", "account"] = "user"        # generic: account
    validate_order_marker_history: bool = False               # generic
    strict_persistence_errors: bool = False                   # generic
    rebuild_on_pending_protected_close: bool = False          # schwab
    exclusive_account_type: bool = False                      # generic (both directions of today's guard)

@dataclass(frozen=True)
class BrokerAdapter:
    broker_type: BrokerType
    account_type: AccountType
    file_extensions: frozenset[str]
    mapper: BrokerParser
    capabilities: BrokerCapabilities
    contract_fixtures: tuple[str, ...]   # paths under tests/test_data, used by the conformance kit
```

- `get_adapter(broker)` and `adapter_for_account_type(account_type)` replace `get_parser`, `_ACCOUNT_TYPE_BROKER` and `_BROKER_ALLOWED_EXTENSIONS`. `BrokerType` moves into the registry module, and `factory.py` re-exports it for one release, or is deleted if no imports remain.
- Flags carry exactly today's broker assignments. Whether `rebuild_on_pending_protected_close` should apply to every broker is recorded as an open question, not decided here.
- Raw-ID scoping becomes generic: with `raw_id_scope="account"`, `{broker}:{id}` is rewritten to `{broker}:{account_id}:{id}`, which yields exactly today's generic IDs.

*Alternative:* polymorphic hooks on the parser (`parser.after_insert(...)`). Rejected: mappers would need DB access, which breaks purity. Flags keep the DB-aware behaviour inside `ImportService`.

### D4 — Mapper contract: `ParseResult` and an optional account-selection step
```python
@dataclass(frozen=True)
class ParseResult:
    records: list[BrokerRawTransactionCreate]
    corporate_actions: list[CorporateActionCreateRequest]
    warnings: list[str]
```

- `BrokerParser` keeps one abstract method, `parse_raw(content, filename) -> ParseResult`. `parse()` and `enrich_pnl_eur` are removed, and parsers hold no mutable state.
- Optional pure hook: `select_account(content, configured_account_number) -> AccountSelection(content, bound_account_number)`. The default is identity. Schwab implements it with `resolve_schwab_account_entry`, which moves today's L373 logic out of `ImportService`.
- `ImportService` applies `bound_account_number` and emits `account.account_number_bound` generically. Today's audit metadata `reason="schwab_single_account_auto_bind"` becomes `reason="single_account_auto_bind"` with `provider=<broker>`, which keeps the metadata safe.
- Shared helpers move to `app/utils/parsers/common.py`: UTC parsing, `safe_decimal`, asset-class mapping helpers. OCC parsing stays in `app/utils/occ.py`, and IBKR's `_extract_occ_underlying` is replaced by `occ.extract_occ_underlying`.

### D5 — Connector contract and one sync flow
`app/services/broker_connectors/` contains `base.py` with the `BrokerConnector` protocol, plus `schwab.py` and `ibkr_flex.py`:

```python
class BrokerConnector(Protocol):
    broker_type: BrokerType
    async def fetch(self, account: Account, window: SyncWindow) -> FetchedPayload: ...

@dataclass(frozen=True)
class FetchedPayload:
    content: bytes            # same format the mapper accepts for uploads
    filename: str
    bound_account_number: str | None = None
```

- Each connector owns authentication. Schwab: OAuth access-token refresh via the existing `BrokerService` helpers, which are passed in. IBKR: Flex token plus query ID from the account properties.
- `SyncWindow` carries `days` and an optional explicit `from_date`.
- `BrokerService.sync_account` performs: fetch → apply binding and audit (shared helper with D4) → `import_trades(broker=connector.broker_type)` → map to `BrokerSyncResult`.
- Exception translation stays per connector: `RateLimitError`/`FlexAPIError` → `UpstreamServiceUnavailableError` or `ConfigurationError`.
- Connectors live in `services/` because they perform I/O. Mappers stay in `utils/`.

### D6 — Migration and backfill (one Alembic revision)
1. Create the enum `positioneffect` and the columns `position_effect` (nullable at first), `order_id` and `net_cash`.
2. Backfill `broker_raw_transactions` in SQL:
   - `order_id = raw_data->>'brokerageOrderID'`
   - `net_cash = (raw_data->>'netCash')::numeric`
   - These are top-level keys only. Generic `source_record` content is nested and therefore excluded, which matches today's repository reads.
   - `position_effect` uses the D1 rule, where `is_mixed` means `raw_data->>'openCloseIndicator'` matches `C;O`/`O;C`.
3. Backfill `logical_executions.position_effect` from each row's own `pnl`, `open_close` and `is_mixed_execution`. Using the row's own values, not its source's, preserves user-edited rows and manual rows.
4. Set the columns to `NOT NULL` and add the partial index.

Downgrade: drop the index, columns and enum. This is lossless because the source values remain in `raw_data`. Batch the updates for large tables. The data is per user, so the volume is small.

### D7 — Conformance kit and golden snapshots
- **Golden snapshots, step 1 of implementation and before any refactor:**
  - A test imports each fixture into a fresh account:
    - IBKR: combined, combo positions, the split fixtures and Flex samples.
    - Schwab: aggregation and short round-trip.
    - The generic parity fixtures.
  - The resulting raw rows (all columns except IDs and timestamps), logical executions and trades are serialised to JSON under `tests/test_data/golden/`.
  - After the refactor the same test must match byte-for-byte, except for the intended new columns, which the test explicitly allows.
- **Contract kit:** `backend/tests/contract/test_broker_adapter_contract.py` is parametrised over `registry.all_adapters()` × `adapter.contract_fixtures`. It asserts the invariants from the spec. It also contains:
  - a static check that `import_service.py` has no `BrokerType.<MEMBER>` references
  - a check that every `BrokerRawTransactionCreate` field appears in the guide's field-reference table

### D8 — Documentation, subagent and skill
- **`docs/broker-integration-guide.md`** contains:
  - a data-flow diagram
  - a field-reference table for every intermediate-model field: meaning, required/optional, mapping hints, and pitfalls such as the commission sign, quantity always positive, and direction from the signed quantity
  - the `position_effect` decision table
  - the mapper and connector contracts
  - a capability-flag catalogue
  - auth patterns, using Schwab OAuth and IBKR Flex as worked examples, plus file-only brokers
  - fixture rules: anonymised, and committed with a converter if they are derived
  - a definition-of-done checklist
- **`.opencode/agents/broker-integration-engineer.md`** (subagent):
  - **Inputs:** the skill's intake bundle.
  - **Outputs:**
    - adapter registration
    - mapper
    - optional connector
    - `AccountType` enum value and migration, via the `db-migration` skill
    - fixtures and golden snapshot
    - mapper unit tests
  - It runs the contract kit, `lint-check` and `unit-test`.
  - It must not touch the aggregator, `ImportService` internals or other mappers. If the broker needs a semantic the contract lacks, it stops and reports the gap instead of adding a `raw_data` key or a broker branch.
  - It hands frontend touch points (account-type option, import-form hint and badge, about 9 files listed in the guide) to `frontend-engineer`.
- **`.opencode/skills/add-broker/SKILL.md`:**
  - **Intake checklist:**
    - broker name and slug
    - delivery modes (file, API or both)
    - file format and an anonymised sample per asset class
    - field documentation or links
    - auth mechanism and credentials shape
  - **Semantics questionnaire:**
    - How does the broker indicate open/close and flips?
    - Does it provide order IDs, stable instrument IDs and realised P&L?
    - What is the timestamp timezone?
    - What is the commission sign?
    - Does one file contain several accounts?
    - Does it report corporate actions?
  - **Escape hatch:** if a broker cannot provide stable execution IDs, recommend the generic format instead of a native adapter.
  - It then invokes the subagent and afterwards `code-reviewer`.
- AGENTS.md "Broker Ingestion Paths" is rewritten around adapter, mapper and connector, and links the guide. The agent table and skill table get new rows.

## Risks / Trade-offs

- **[Hidden behavioural drift in the aggregator switch]** → Golden snapshots are taken before any code change. The D1 helper is shared by mappers and the migration. The contract kit checks legacy equivalence for the existing adapters.
- **[Two overlapping fields (`open_close`, `position_effect`)] confuse new mappers** → The guide states that `position_effect` drives FIFO and `open_close` is only the broker indicator used for combo marker consistency. A diagnostic SQL in the tasks measures rows where they disagree, which informs the merge follow-up.
- **[Synthetic `pnl=0` stays]** → It no longer has a closing meaning. The pnl-calculation spec restricts its numeric meaning. Removal is a follow-up with its own parity analysis for orphan and combo P&L.
- **[Large `ImportService` diff collides with in-flight work]** → Implement in the phase order of `tasks.md`. Each phase is green on its own: snapshots, model and migration, aggregator switch, registry and flags, mapper contract, connectors, legacy removal, docs and tooling.
- **[Agent oversteps the contract]** → The agent's hard rules plus the static contract checks, with `code-reviewer` mandatory in the skill flow.
- **[Enum migration on big tables]** → Backfill in batches. A downgrade is available and lossless.

## Migration Plan

1. Merge snapshots, the model and migration, and the aggregator switch together. After deploy, run the migration, then verify with the diagnostic query that the `position_effect` distribution matches the legacy rule (zero mismatches).
2. Deploy the registry, the contracts and the legacy removal. No data change.
3. Rollback: revert the code. The Alembic downgrade drops the new columns, and `raw_data` still contains the source values, so older code keeps working.

## Open Questions

- Should `rebuild_on_pending_protected_close` become unconditional for every broker? It is Schwab-only today, and IBKR could hit the same residue state.
- Follow-ups:
  - merge `open_close` into `position_effect` after the disagreement diagnostic
  - drop synthetic `pnl=0`
  - Schwab order and instrument IDs (combos)
  - a frontend broker list served from the registry via `GET /imports/brokers`
