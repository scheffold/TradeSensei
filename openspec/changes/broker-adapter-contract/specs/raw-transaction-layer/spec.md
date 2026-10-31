## ADDED Requirements

### Requirement: First-class pipeline fields on raw transactions and logical executions
`BrokerRawTransaction` SHALL store `order_id` (nullable string), `net_cash` (nullable decimal) and `position_effect` (non-null enum `OPEN | CLOSE | FLIP | UNKNOWN`) as columns. `LogicalExecution` SHALL store `position_effect` (non-null). When a logical execution is materialised from a raw transaction it SHALL copy `position_effect`, and SHALL set `is_mixed_execution` to true exactly when `position_effect=FLIP`. Executions created manually (leg editor, partial/full close, split, merge) SHALL set `position_effect=UNKNOWN` in this change, which preserves their current aggregation behaviour. Split and merge results SHALL inherit the effect of their source execution. `raw_data` SHALL NOT be read by any pipeline component to derive grouping, cash or position effect.

#### Scenario: Materialising a flip
- **WHEN** a raw transaction with `position_effect=FLIP` is materialised
- **THEN** the logical execution SHALL have `position_effect=FLIP` and `is_mixed_execution=true`

#### Scenario: Manual full close
- **WHEN** a user fully closes a trade via the partial-close flow
- **THEN** the created manual execution SHALL have `position_effect=UNKNOWN`

#### Scenario: Split inherits position effect
- **WHEN** an imported execution with `position_effect=CLOSE` is split
- **THEN** every child execution SHALL have `position_effect=CLOSE`

#### Scenario: Combo metadata lookup
- **WHEN** combo order metadata is requested for raw transaction IDs
- **THEN** the repository SHALL return the `order_id` and `net_cash` columns without reading `raw_data`
