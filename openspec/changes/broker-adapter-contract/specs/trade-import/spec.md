## MODIFIED Requirements

### Requirement: All broker ingestion terminates in the unified import service

Every path by which broker data enters the system SHALL terminate in `ImportService.import_trades()`. This covers manual file upload, scheduled account sync, and direct broker API pull. A broker-specific connector MAY authenticate and fetch a payload from a remote API, but SHALL NOT parse, aggregate, deduplicate, or persist trade data itself. The fetched payload SHALL be passed in the same format the broker's registered mapper accepts for file uploads. No ingestion path may create a `Trade` row directly. API syncs for all brokers SHALL run through one broker-independent sync flow in `BrokerService`.

This exists because a parallel ingestion implementation silently misses every protection, invariant, and bug fix applied to the primary path. The IBKR Flex sync did exactly that and produced leg-less phantom trades in production while the primary path was demonstrably correct.

#### Scenario: Remote-fetch path delegates rather than persisting

- **WHEN** a broker sync retrieves a payload from a broker API
- **THEN** it SHALL pass the payload to `ImportService.import_trades()` with the matching `BrokerType`
- **THEN** it SHALL NOT call any trade or execution repository write method itself

#### Scenario: File upload and API sync are behaviourally identical

- **GIVEN** the same broker payload delivered once by file upload and once by API sync
- **WHEN** both are imported into the same account
- **THEN** the resulting trades, executions, and raw transactions SHALL be identical
- **THEN** the second ingestion SHALL be a no-op through raw-transaction idempotency

#### Scenario: No ingestion path creates a trade without executions

- **WHEN** any ingestion path creates a `Trade`
- **THEN** that trade SHALL have at least one linked `LogicalExecution`, or the execution SHALL be reported as unlinked per the execution-overwrite-protection capability

#### Scenario: Sync flow is shared across brokers

- **WHEN** an IBKR or Schwab account is synced
- **THEN** `BrokerService` SHALL obtain the payload from the broker's registered connector and run the same fetch–import–result-mapping flow for both brokers
