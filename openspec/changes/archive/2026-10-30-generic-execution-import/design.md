## Context

Broker data enters the system through exactly one pipeline: `ImportService.import_trades()` → `BrokerParser.parse_raw()` → `BrokerRawTransactionCreate` → `LogicalExecution` → FIFO/combo aggregation → `Trade`. Two parsers exist (`IBKRXMLParser`, `SchwabJSONParser`); the parser is chosen from the account type in `app/api/v1/imports.py` (`_ACCOUNT_TYPE_BROKER`, `_BROKER_ALLOWED_EXTENSIONS`).

`BrokerRawTransactionCreate` is already a broker-neutral, execution-level record. The generic format is therefore essentially a documented, user-facing serialisation of that DTO.

Pipeline facts that shape the design (verified in code):

- **Closing legs are identified by `pnl is not None`**, not by `open_close` (`aggregator.py`, `is_closing_leg = exec_trade.pnl is not None`). Schwab sets `pnl=0` for closing executions for exactly this reason.
- **Combo metadata is read from IBKR-named keys in `raw_data`**: `BrokerRawTransactionRepository.get_combo_order_meta_by_raw_ids` reads `raw_data["brokerageOrderID"]` and `raw_data["netCash"]`.
- **Position flips** are detected from `raw_data["openCloseIndicator"]` containing `C;O`/`O;C` (`logical_execution_service.py`). None of the current fixtures contain a flip.
- Raw IDs are stamped as `{user_id}:{rec.id}` in `ImportService`; inserts use `ON CONFLICT (id) DO NOTHING`.
- Lot key in the aggregator is `(symbol, conid, asset_class)`; Schwab leaves `conid=None`.
- `AccountType` is a PostgreSQL enum `accounttype` (`create_type=False`).

## Goals / Non-Goals

**Goals:**
- One documented v1 format, two encodings (CSV, JSON), one parser, zero changes to aggregation logic.
- Re-imports are always safe (mandatory `execution_id`, account-scoped idempotency).
- Users get actionable errors for every invalid row in one round trip.
- Native and generic imports of the same data are provably equivalent (parity tests on existing fixtures).

**Non-Goals:**
- XML encoding.
- Trade-level (round-trip) records — users write two execution rows instead.
- Column mapping / per-broker presets in the UI ("map my broker's columns").
- Corporate actions in CSV (JSON only).
- Explicit position-flip marker; users split a flip into a `CLOSE` row and an `OPEN` row.
- Promoting `brokerageOrderID`/`netCash`/`openCloseIndicator` to first-class columns (follow-up, see Open Questions).
- Updating already-imported executions via re-import.

## Decisions

### D1 — Execution-level records, mapped 1:1 onto `BrokerRawTransactionCreate`
A Pydantic model `GenericExecutionV1` (new module `app/schemas/generic_import.py`) validates one record and exposes `to_raw()` returning a `BrokerRawTransactionCreate`. CSV rows and JSON objects are both reduced to `dict[str, object]` by thin encoding adapters before validation, so all validation lives in one model.
*Alternative:* trade-level rows (entry/exit per line). Rejected: bypasses FIFO, partial closes, combos and cross-file matching; would need a second aggregation path.

### D2 — `GenericParser` implements `parse_raw()` only
`app/utils/parsers/generic.py`. `parse()` (abstract, legacy) raises `NotImplementedError`; `ImportService` already prefers `parse_raw()`. Registered as `BrokerType.GENERIC = "generic"` in `factory.py`.
*Alternative:* a separate `GenericImportService`. Rejected by the "single ingestion pipeline" rule (AGENTS.md §5).

### D3 — Closing semantics via `pnl`
`pnl = realized_pnl` if present, else `Decimal("0")` if `open_close=CLOSE`, else `None`. This mirrors the Schwab parser and lets the existing aggregator recognise closing legs (including orphan closes) without modification. `open_close` is still mapped to `"O"`/`"C"` and stored.
Documentation states clearly: mark closing rows with `open_close=CLOSE`, especially for positions opened before the file's period.

**Review clarification (user approved):** the combo builder sums reported closing
P&L and treats the zero marker as an actual reported zero. Therefore every
order-linked row requires an explicit OPEN/CLOSE marker, and every order-linked
CLOSE requires `realized_pnl`; a genuine breakeven close explicitly supplies 0.
Rows sharing an order ID in a file must agree on their marker. Ordinary FIFO
closes without order metadata retain the zero-marker mapping. No combo P&L
algorithm is changed.

An explicitly OPEN row with reported P&L is contradictory and must be rejected,
including when the supplied P&L is zero. Order marker consistency also checks
existing generic raw transactions for the target account under its import lock,
before inserting anything. Reuse an order ID across exports of the same order;
opening and closing orders must use different IDs.

### D4 — Combo metadata via existing `raw_data` keys
When `order_id` is present the parser writes `raw_data["brokerageOrderID"] = order_id` and `raw_data["netCash"] = ±(quantity × price × multiplier) − commission` (sign: SELL `+`, BUY `−`), matching IBKR's `netCash` convention. This keeps the repository and combo builder untouched.
*Alternative:* add `order_id`/`net_cash` columns to `broker_raw_transactions`. Cleaner, but requires a migration and IBKR parser changes; deferred (Open Questions).
The key names are defined once as constants in `app/core/constants.py` with a
comment pointing at the repository that reads them. Sharing them from core lets
the DTO's `to_raw()` mapping use them without a schema/parser circular import.

**Implementation clarification (user approved):** the existing combo builder
requires at least two distinct, non-null `conid` values, not just an order ID.
Keep `instrument_id` optional for ordinary non-derivative and contract-specific
OCC-symbol fills, but require a stable identifier for each distinct combo contract.
Within a file, group records by `order_id`;
when their `(symbol, asset_class, expiry, strike, option_type)` descriptions differ,
report any missing identifiers and any identifier reused for differing contracts
as safe `instrument_id` row errors. Multiple fills of the same contract may share
its identifier. Cross-file single-leg uploads must supply identifiers proactively
because a stateless parser cannot infer the other contracts. No fallback IDs and
no aggregation changes are introduced. Templates and documentation show explicit,
distinct instrument IDs and consistent `open_close` markers for combo legs.

**Trusted metadata boundary:** preserve the entire original record under
`raw_data["source_record"]`, converting Decimal literals losslessly to JSON-safe
strings. Only validated v1 fields may populate active top-level pipeline keys.
Unknown legacy control names (`brokerageOrderID`, `netCash`, `openCloseIndicator`)
remain inert in the nested source payload; they must never supply combo or flip
semantics. This retains source data without creating another ingestion format.

**Ordinary derivative identity (user approved):** FIFO uses
`(symbol, conid, asset_class)`, not strike/expiry/type. Require `instrument_id`
for FUTURE and plain-symbol OPTION fills; standalone valid OCC-symbol options
may omit it because their symbol identifies the contract. Combos still require
explicit distinct IDs, including OCC-symbol legs. Do not synthesize IDs or alter
FIFO. Reject ambiguous records at the format boundary.

Providing `order_id` opts into multi-leg metadata, so every such row requires an
explicit instrument ID, including separately uploaded OCC legs. The ID-free OCC
exception applies only without order metadata. Explicit contract fields must
agree with the OCC symbol; they cannot override its encoded identity. Across
exports, preserve symbol spelling and ID presence/values consistently.

### D5 — Account-scoped idempotency key
The parser emits `id = "generic:{execution_id}"` (it has no account context). `ImportService.import_trades()` rewrites generic IDs to `generic:{account_id}:{execution_id}` before the existing `{user_id}:` stamping, giving `{user_id}:generic:{account_id}:{execution_id}`. Max length: 36+1+7+1+36+1+100 = 182 ≤ 200 (`_EXEC_ID_MAX_LENGTH`). Generic imports require `account_id` (always true for uploads to a `GENERIC` account).
*Alternative:* hash fallback when `execution_id` is missing. Rejected by the user: identical fills in the same second collapse and re-imports corrupt trades.

### D6 — New `AccountType.GENERIC`
Generic files are accepted only by `GENERIC` accounts; `GENERIC` accounts accept only `.csv`/`.json`. This prevents mixing ID namespaces (`ibkr:` vs `generic:`) on one account, which would double-count executions. Alembic migration: `ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'generic'` (must run outside a transaction block — use `op.get_context().autocommit_block()`). Downgrade is a no-op with a comment (Postgres cannot drop enum values safely).
*Alternative:* allow generic files on `MANUAL` accounts. Rejected: manual accounts hold hand-entered trades without raw transactions; mixing complicates "clear trades" and protection semantics.

Guard type transitions to/from generic whenever raw/logical/trade history exists,
even on credentialless accounts or through manual type. Check history in a
repository and serialize the transition with account-targeted ingestion; require
a new or explicitly cleared account before switching namespaces. Metadata-protection
queries must also be scoped to the target account, not merely its portfolio, so
a protected conid-less fill in one account cannot suppress another account's trade.

Protection membership and existing-trade fallback matching must consistently use
`(account_id, symbol, asset_class, broker_conid, opened_at)` (execution anchors still
take precedence where available). Account filtering alone does not distinguish
different null-conid instruments at one timestamp inside the same account.

### D7 — Format detection by content, not extension
First non-whitespace byte after optional BOM: `{` → JSON, `<` → reject (XML unsupported), else CSV. The extension check in `imports.py` still restricts uploads to `.csv`/`.json`.
JSON is decoded with `json.loads(..., parse_float=Decimal, parse_int=Decimal)` so numeric literals never pass through `float`. CSV is read with `csv.DictReader` on `utf-8-sig` decoded text.

### D8 — All-or-nothing validation with structured errors
The parser validates all records, collecting `GenericRowError(location, field, message)` (location = CSV line number incl. header, or `executions[i]`). Messages are built from field names and Pydantic error types only — never echoing values (audit policy: no prices/quantities/P&L). If any error exists it raises `GenericImportValidationError(ImportParseError)` carrying `errors`. The upload route catches it before the generic `ImportParseError` handler and returns HTTP 400 with `detail = {"message": ..., "errors": [...]}`; existing string `detail` for other errors is unchanged. Error list is capped (e.g. first 200) with a "N more" marker.
`ImportForm` renders the list when `detail` is an object.

Use a single Counter for CSV header frequencies, not repeated list scans. Convert
UTC normalization overflow into safe field errors so remaining rows are still
validated. Corporate-action ratios must fit Numeric(18,8), preventing positive
subscale values from persisting as zero. The generated input schema must describe
pre-normalization enum casing and numeric multiplier strings, not just the
normalized Python values.

### D9 — Schema and template endpoints
New router `app/api/v1/generic_import.py` mounted at `/api/v1/imports/generic` (authenticated, not portfolio-scoped):
- `GET /schema` → `GenericImportDocumentV1.model_json_schema()` as `application/schema+json`.
- `GET /template.csv` → static CSV built from the field list plus example rows; a test imports it into an empty generic account.
Not audited at INFO (GET).

### D10 — Parity fixtures via a conversion tool
`tools/convert_to_generic.py` reads a native fixture, runs the native parser's `parse_raw()`, and serialises each `BrokerRawTransactionCreate` back to v1:
`execution_id` = native ID without the broker prefix, `instrument_id` = `conid`, `order_id` = `raw_data["brokerageOrderID"]`, `realized_pnl` = `pnl`, `open_close`, contract fields, UTC `Z` timestamps. Output is deterministic (stable ordering, fixed decimal formatting). Generated files are committed under `tests/test_data/generic/` (`ibkr_combined.{csv,json}`, `ibkr_combo_positions.{csv,json}`, `schwab_aggregation.{csv,json}`, `schwab_short_roundtrip.{csv,json}`). A test re-runs the converter and asserts byte equality.
Parity tests import native into account A and generic into account B (same user/portfolio fixture setup as `test_schwab_json_import.py`) and compare trades on the fields listed in the spec, ignoring IDs, account and raw IDs. The tool doubles as a reference implementation for users writing their own converters (linked from the README).

### D11 — Documentation placement
- `docs/generic-import-format.md`: full reference (the README requested).
- `frontend/lib/help-content.ts`: new section "Importing from other brokers" with entries *Generic import format*, *execution_id & re-imports*, *Closing rows & open_close*, *Multi-leg orders*; regenerate `docs/HELP_GLOSSARY.md` via `tools/export_help.ts`.
- `docs/api-usage.md`: upload example for a generic account and the two download endpoints.

## Risks / Trade-offs

- [Raw-data key coupling (D4)] Generic parser depends on IBKR key names read by the repository → Constants with cross-reference comment; parity test on `combo_positions.xml` fails if the contract drifts; follow-up to promote to columns.
- [Derived `netCash` may differ from the IBKR-reported value by rounding] → Parity compares `net_cash` rounded to 2 decimals. The format does not get a `net_cash` field just to make parity exact.
- [Users omit `open_close=CLOSE` for positions opened before the file] → Row becomes a new opposite position instead of an orphan close. Mitigated by prominent documentation (README, help, template example); no heuristic warning in v1.
- [Correcting a wrong row] Re-import doesn't update existing executions (`ON CONFLICT DO NOTHING`) → Document: fix via execution editor, or "Clear All Trades" and re-import.
- [IBKR fixture parity may expose IBKR-specific behaviour not representable in v1 (e.g. `pnl=None` with `open_close="C"`)] → Converter preserves `realized_pnl` explicitly; any residual mismatch is analysed and either documented as a known limitation or fixed in the mapping before merge.
- [Enum migration is irreversible] → Downgrade no-op; value unused until a `GENERIC` account is created.

### Known parity limitation: broker-reported cash overrides

The native `combo_positions.xml` fixture contains one internally inconsistent cash
value: execution `900000011` is a BUY-to-close of one contract at `63.03` with
multiplier `100` and commission `0.69028`, but reports `netCash="3153.0"`.
The v1 formula necessarily produces `-6303.69028` instead. This is not a rounding
difference and cannot be preserved without adding a cash-override field, which
is outside v1. The native fixture and parser remain unchanged. Parity tests must
assert this specific raw-metadata difference explicitly and still require exact
parity for all specified trade fields, P&L, and combo leg membership; all other
order cash values must match to two decimal places. Closing cash metadata does
not determine combo direction (opening cash does).

## Migration Plan

1. Deploy migration adding `generic` to `accounttype`.
2. Deploy backend (parser, routes); regenerate OpenAPI client.
3. Deploy frontend (account type option, import form, help).
Rollback: revert application code; the extra enum value is harmless. Existing generic accounts would become unusable for import until redeploy.

## Open Questions

- Promote `order_id`, `net_cash` and a flip indicator to first-class `broker_raw_transactions` columns (benefits Schwab combos too)? Proposed as a separate change.
