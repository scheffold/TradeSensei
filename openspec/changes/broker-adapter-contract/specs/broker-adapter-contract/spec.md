## ADDED Requirements

### Requirement: Broker adapter registry
Every supported broker SHALL be described by exactly one adapter registration that declares its `BrokerType`, the `AccountType` it serves, the allowed upload file extensions, its mapper (parser) and its capability flags. The upload endpoint, `ImportService` and `BrokerService` SHALL obtain this information only from the registry. Broker-specific behaviour inside `ImportService` SHALL be selected by capability flags, not by comparing against concrete `BrokerType` values.

#### Scenario: Upload validation uses the registry
- **WHEN** a file is uploaded to an account
- **THEN** the accepted extensions and the selected mapper SHALL be those registered for the account's type

#### Scenario: Unregistered broker
- **WHEN** an import is requested for a broker type without a registration
- **THEN** the system SHALL raise `UnsupportedBrokerError` before parsing

#### Scenario: Import service contains no broker type comparisons
- **WHEN** the source of `app/services/import_service.py` is inspected by the conformance test kit
- **THEN** it SHALL contain no comparison against a concrete `BrokerType` member

#### Scenario: Capability flags preserve existing broker behaviour
- **WHEN** IBKR, Schwab and generic fixtures are imported after the refactor
- **THEN** account-scoped raw IDs, history-based order-marker validation, strict persistence errors and the pending-protected-close rebuild SHALL apply to exactly the same brokers as before the refactor

### Requirement: Mapper contract
A broker mapper SHALL be a pure function from raw bytes and a filename to a `ParseResult` containing raw execution records (`BrokerRawTransactionCreate`), corporate actions and warnings. A mapper SHALL NOT access the database, the network, or mutable instance state. It SHALL NOT aggregate, deduplicate across files, or persist data. Each record SHALL carry an ID of the form `{broker_type}:{source_execution_id}`, timezone-aware UTC `executed_at`, a positive `quantity`, and an explicit `position_effect`.

#### Scenario: Mapping is deterministic
- **WHEN** the same content is mapped twice
- **THEN** both `ParseResult` values SHALL be equal

#### Scenario: Warnings are returned, not stored
- **WHEN** a mapper skips an invalid source row
- **THEN** the warning SHALL be part of the returned `ParseResult` and SHALL NOT be read from a parser attribute

#### Scenario: Unparseable content
- **WHEN** content cannot be decoded in the broker's format
- **THEN** the mapper SHALL raise `ImportParseError`

### Requirement: Intermediate execution model semantics
`BrokerRawTransactionCreate` SHALL be the single broker-neutral intermediate model. All fields the pipeline interprets SHALL be first-class fields:
- `position_effect`: `OPEN`, `CLOSE`, `FLIP` (one execution that closes and reopens in the opposite direction) or `UNKNOWN` (the broker states no effect)
- `order_id`: groups executions of one broker order
- `net_cash`: signed order cash (sell positive, buy negative, commission deducted)
- `conid`: stable broker instrument identifier

`raw_data` SHALL preserve the source record for traceability only, and no pipeline component SHALL read semantics from it.

#### Scenario: Combo metadata from first-class fields
- **WHEN** the import pipeline builds combo positions
- **THEN** order grouping and cash SHALL be read from `order_id` and `net_cash`, not from `raw_data`

#### Scenario: Flip from first-class field
- **WHEN** a logical execution is materialised from a raw transaction with `position_effect=FLIP`
- **THEN** it SHALL be marked as a mixed execution regardless of `raw_data` content

#### Scenario: Source keys are inert
- **WHEN** a raw transaction's `raw_data` contains keys named `brokerageOrderID`, `netCash` or `openCloseIndicator` but the first-class fields are empty
- **THEN** grouping, cash and flip handling SHALL behave as if those keys were absent

### Requirement: Connector contract for API pulls
A broker that supports API sync SHALL provide a connector that authenticates with broker-specific credentials and returns a `FetchedPayload` (content bytes in the same format the mapper accepts for uploads, a filename, and optionally an account number to bind). Connectors SHALL NOT parse, aggregate or persist trade data. `BrokerService` SHALL run one broker-independent sync flow: fetch, apply the optional account-number binding with its audit event, call `ImportService.import_trades()` with the adapter's broker type, and map the result to `BrokerSyncResult`.

#### Scenario: Sync and upload share the mapper
- **WHEN** a connector's payload and an uploaded file contain the same executions
- **THEN** both SHALL be mapped by the same registered mapper and produce identical raw transactions

#### Scenario: Account number auto-binding is generic
- **WHEN** a connector returns `bound_account_number` for an account without an account number
- **THEN** the sync flow SHALL store it and emit the `account.account_number_bound` audit event, independent of the broker

#### Scenario: Authentication failure stays broker-specific
- **WHEN** a connector cannot authenticate
- **THEN** it SHALL raise a domain exception (`ConfigurationError` or `UpstreamServiceUnavailableError`) and no import SHALL start

### Requirement: Conformance test kit
The test suite SHALL contain a contract test module parametrised over every registered adapter and every fixture that the adapter registers. It SHALL assert the mapper contract and intermediate-model invariants: ID prefix and in-file uniqueness, UTC timestamps, positive quantities, non-null `position_effect`, deterministic output, JSON-serialisable `raw_data`, `order_id` rows carrying `conid`, and no `OPEN` record with non-null `pnl`. A registered adapter without fixtures SHALL fail the suite.

#### Scenario: New broker is covered automatically
- **WHEN** a new adapter and its fixtures are registered
- **THEN** the contract tests SHALL run for it without modifying the test module

#### Scenario: Adapter without fixtures
- **WHEN** an adapter is registered without contract fixtures
- **THEN** the conformance suite SHALL fail with a message naming the adapter

### Requirement: Behaviour-neutral migration of existing data
Introducing the first-class fields SHALL NOT change any existing trade projection. The migration SHALL backfill:
- `order_id` and `net_cash` from `raw_data["brokerageOrderID"]` and `raw_data["netCash"]`, excluding values nested in `source_record`
- `position_effect` with today's effective semantics: `FLIP` for mixed executions with non-null `pnl`, `CLOSE` for other rows with non-null `pnl`, `OPEN` for rows with null `pnl` and `open_close='O'`, otherwise `UNKNOWN`

#### Scenario: Rebuild after migration is a no-op
- **WHEN** the projection of an account is rebuilt after the migration
- **THEN** the trades SHALL equal the golden snapshot taken before the migration

#### Scenario: Unknown effect behaves like today's null-pnl rows
- **WHEN** an execution has `position_effect=UNKNOWN`
- **THEN** the aggregator SHALL treat it like an opening-or-netting execution, matched FIFO by direction, without broker-reported closing semantics

### Requirement: Broker integration guide and tooling
The repository SHALL contain `docs/broker-integration-guide.md`. It SHALL describe:
- the data flow
- every intermediate-model field and its semantics
- the mapper and connector contracts
- capability flags
- authentication patterns (OAuth with refresh, static token plus query ID, file-only)
- the fixture and conformance-test requirements
- a step-by-step checklist for adding a broker

The repository SHALL also provide a `broker-integration-engineer` subagent and an `add-broker` skill that gathers the required broker information before invoking the subagent.

#### Scenario: Guide matches the model
- **WHEN** a field is added to or removed from `BrokerRawTransactionCreate`
- **THEN** the conformance test kit SHALL fail unless the field is documented in the guide's field reference

#### Scenario: Skill collects inputs before implementation
- **WHEN** the `add-broker` skill is invoked
- **THEN** it SHALL collect the inputs below and pass them to the subagent:
  - broker name
  - delivery modes (file, API or both)
  - anonymised sample exports
  - field documentation
  - authentication mechanism
  - answers to the semantics questionnaire (position effect, order IDs, instrument IDs, P&L, timezone, commission sign)
