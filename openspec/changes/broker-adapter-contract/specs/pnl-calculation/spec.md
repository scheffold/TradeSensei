## REMOVED Requirements

### Requirement: Broker PnL Presence As A Closing-Leg Signal Is Aggregator-Internal
**Reason**: The closing-leg decision moves to the explicit `position_effect` field; no component, including the FIFO aggregator, may infer it from `pnl` presence any more.
**Migration**: Use `position_effect` (`CLOSE`/`FLIP` = closing). Existing rows are backfilled by the `broker-adapter-contract` migration with the semantics they had before.

## ADDED Requirements

### Requirement: Closing-Leg Semantics Come From Position Effect
The FIFO aggregator, the conversion from logical executions to trade rows and the unregistered-split warning SHALL treat an execution as a closing leg exactly when its `position_effect` is `CLOSE` or `FLIP`. No component SHALL infer opening/closing semantics, or the availability of a broker-reported value, from the presence or absence of `pnl`. A `pnl` value that a parser emits only as a closing marker (synthetic zero) SHALL carry no numeric meaning outside the existing combo P&L sum for order-linked closes.

#### Scenario: Closing leg without reported P&L
- **WHEN** an execution has `position_effect=CLOSE` and `pnl=None`
- **THEN** the aggregator SHALL treat it as a closing leg and derive Gross PnL from prices

#### Scenario: Reported P&L on an unknown-effect execution
- **WHEN** an execution has `position_effect=UNKNOWN` and a non-null `pnl`
- **THEN** the aggregator SHALL NOT treat it as a closing leg because of the `pnl` value

#### Scenario: Re-Aggregation Encounters A Sentinel
- **WHEN** a trade is re-aggregated and one of its executions carries a sentinel `pnl`
- **THEN** the sentinel SHALL NOT be interpreted as a broker-reported PnL of zero
- **AND** the trade's PnL SHALL be derived from execution prices
