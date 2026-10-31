## MODIFIED Requirements

### Requirement: Mapping onto the unified import pipeline
Generic imports SHALL be processed by `ImportService.import_trades()` with `BrokerType.GENERIC`. The parser SHALL implement the mapper contract of the `broker-adapter-contract` capability: it returns a `ParseResult` with one `BrokerRawTransactionCreate` per record, the list of corporate actions and the warnings. It SHALL NOT aggregate, deduplicate or persist data. The mapping SHALL be:

- `side` `BUY` → `direction=LONG`, `SELL` → `direction=SHORT`
- `open_close` `OPEN` → `"O"`, `CLOSE` → `"C"`, empty → `None`
- `position_effect` = `OPEN` when `open_close=OPEN`; `CLOSE` when `open_close=CLOSE`, or when `open_close` is empty and `realized_pnl` is present; otherwise `UNKNOWN`
- `instrument_id` → `conid` (empty → `None`)
- `pnl` = `realized_pnl` if present; otherwise `0` when `open_close=CLOSE`; otherwise `None`. The synthetic zero is retained only for output parity and carries no closing semantics.
- `order_id` → first-class `order_id`
- `net_cash` (first-class) = `±(quantity × price × multiplier) − commission` (SELL `+`, BUY `−`), set only when `order_id` is present
- `raw_data["source_record"]` = the original record as read from the file, retained losslessly in a JSONB-safe representation. `raw_data` SHALL contain no other pipeline keys.
- Unknown fields, including legacy `brokerageOrderID`, `netCash` and `openCloseIndicator`, SHALL remain inert inside `source_record` and SHALL NOT affect order grouping, cash, position effect or mixed-execution flags.

#### Scenario: Closing execution without realized_pnl is treated as closing
- **WHEN** a record has `open_close=CLOSE`, no `order_id` and no `realized_pnl`
- **THEN** the raw transaction SHALL have `position_effect=CLOSE` and the aggregator SHALL treat it as a closing leg

#### Scenario: Orphan close without an open lot
- **WHEN** a `SELL` record with `open_close=CLOSE` has no matching open lot in the database or the file
- **THEN** the aggregator SHALL reconstruct an orphaned closed trade exactly as for native broker imports

#### Scenario: Multi-leg order grouped as a combo
- **WHEN** two option records share `order_id=ord-77`, the same `executed_at`, a consistent `open_close` marker and distinct, non-empty `instrument_id` values
- **THEN** the combo builder SHALL group them as one combo position, as it does for IBKR combo orders

#### Scenario: Multi-leg order missing contract identifiers is rejected
- **WHEN** a file contains distinct contracts sharing `order_id` and any leg omits `instrument_id`
- **THEN** the import SHALL fail before persistence, naming the affected record and `instrument_id` field

#### Scenario: Multi-leg contracts reuse one identifier
- **WHEN** distinct contracts sharing `order_id` use the same `instrument_id`
- **THEN** the import SHALL fail before persistence, naming each conflicting record and `instrument_id` field

#### Scenario: Order-linked records require consistent position effects
- **WHEN** a record has `order_id` but no `open_close`, or records sharing an order ID use mixed OPEN/CLOSE markers within a file or against existing target-account raw transactions
- **THEN** the file SHALL be rejected before persistence with safe `open_close` errors instead of importing raw rows without trade projections

#### Scenario: Single-leg order uploads require contract identifiers
- **WHEN** a record has `order_id` but no `instrument_id`, including an OCC-symbol option uploaded separately from its other legs
- **THEN** the file SHALL be rejected with an `instrument_id` field error before persistence

#### Scenario: Contradictory opening P&L rejected
- **WHEN** a record explicitly has `open_close=OPEN` and a non-null `realized_pnl`, including zero
- **THEN** the record SHALL be rejected with a safe `realized_pnl` error, never silently classified as an orphan close

#### Scenario: Explicit option fields contradict an OCC symbol
- **WHEN** explicit expiry, strike or option type disagrees with a valid OCC symbol
- **THEN** the record SHALL be rejected before persistence, naming the conflicting fields

#### Scenario: Order-linked closes require explicit reported P&L
- **WHEN** a CLOSE record has `order_id` but no `realized_pnl`
- **THEN** the file SHALL be rejected with a `realized_pnl` field error, never silently reporting a breakeven combo

#### Scenario: Unknown native metadata is inert
- **WHEN** otherwise valid CSV or JSON contains unknown legacy pipeline-control fields
- **THEN** its trade grouping, direction, cash, position effect and logical mixed-execution flags SHALL equal the import without those extras
- **THEN** the original extras SHALL remain available inside `raw_data.source_record` only

#### Scenario: Order metadata stored in first-class fields
- **WHEN** a record with `order_id` is imported
- **THEN** the raw transaction SHALL have `order_id` and `net_cash` columns set and `raw_data` SHALL contain only `source_record`
