## ADDED Requirements

### Requirement: Generic execution format v1 record
The system SHALL accept a broker-neutral execution format (schema version `1`) in which each record represents exactly one execution (fill). Each record SHALL consist of the following fields:

| Field | Required | Format |
|---|---|---|
| `execution_id` | yes | non-empty string, max 100 characters |
| `executed_at` | yes | ISO 8601 date-time **with** UTC offset or `Z` |
| `symbol` | yes | non-empty string |
| `asset_class` | yes | `EQUITY`, `OPTION`, `FUTURE`, `FOREX`, `CRYPTO` |
| `side` | yes | `BUY`, `SELL` |
| `quantity` | yes | decimal > 0 |
| `price` | yes | decimal ≥ 0, price per unit without multiplier |
| `currency` | yes | ISO 4217 code (3 uppercase letters) |
| `commission` | no | decimal ≥ 0, default `0`, all fees summed as an absolute value |
| `open_close` | when `order_id` is present | `OPEN`, `CLOSE`, or empty for ordinary fills without order metadata |
| `multiplier` | for `OPTION`/`FUTURE` | integer ≥ 1; defaults to `1` for other asset classes |
| `instrument_id` | for `FUTURE`, plain-symbol `OPTION`, and any `order_id` | string, max 50 characters, stable contract identifier; distinct contracts require distinct identifiers |
| `expiry` | for `OPTION` | `YYYY-MM-DD`; optional for `FUTURE` |
| `strike` | for `OPTION` | decimal > 0 |
| `option_type` | for `OPTION` | `C`, `P` |
| `order_id` | no | string; groups the legs of one multi-leg order |
| `realized_pnl` | for `CLOSE` with `order_id` | decimal, broker-reported realised P&L of a closing execution; optional otherwise |

Enumerated values SHALL be matched case-insensitively. Decimal values SHALL use `.` as decimal separator and SHALL NOT contain thousands separators.

`instrument_id` SHALL remain optional for ordinary EQUITY, FOREX and CRYPTO fills
and ordinary OPTION fills whose valid OCC symbol uniquely identifies the contract.
FUTURE fills and OPTION fills using a plain symbol SHALL require a stable,
non-empty `instrument_id`, because the unchanged FIFO key does not include expiry,
strike or option type. Every distinct contract in a multi-leg combo order SHALL
also have a stable, non-empty `instrument_id`, reused
for that contract on opening and closing fills and across files. When a file
contains distinct contract descriptions sharing `order_id`, the parser SHALL
reject missing identifiers or one identifier assigned to different contracts,
reporting the affected records' `instrument_id` fields. Separate fills of the
same contract MAY share its identifier. The parser cannot infer a multi-leg
order from a file containing only one of its contracts; callers MUST still
provide the identifiers when uploading legs separately.

Every record with `order_id` opts into multi-leg metadata and SHALL supply both
`instrument_id` and `open_close`, even when only one leg appears in the current
file. The ID-free OCC exception applies only without `order_id`. All records
sharing an order ID SHALL use the same OPEN/CLOSE marker, including existing
generic raw transactions in the target account. Every order-linked
CLOSE record SHALL supply `realized_pnl`, including when the file contains only
one contract of the order. The pipeline sums reported closing P&L for combos and
cannot infer an omitted value. Genuine breakeven closes MAY explicitly supply 0.
An explicitly OPEN record SHALL NOT supply `realized_pnl`, including zero, because
non-null reported P&L identifies a closing execution in the unchanged pipeline.
Explicit option contract fields SHALL agree with any valid OCC symbol.

#### Scenario: Minimal equity record is accepted
- **WHEN** a record contains only `execution_id`, `executed_at`, `symbol`, `asset_class=EQUITY`, `side`, `quantity`, `price` and `currency`
- **THEN** the record SHALL be accepted with `commission=0` and `multiplier=1`

#### Scenario: Option record with explicit contract fields is accepted
- **WHEN** a record has `asset_class=OPTION`, `multiplier=100`, `expiry`, `strike`, `option_type` and a stable `instrument_id` (or a valid contract-specific OCC symbol)
- **THEN** the record SHALL be accepted and the contract fields SHALL be stored on the raw transaction

#### Scenario: Plain-symbol derivatives require contract identity
- **WHEN** a FUTURE record or a plain-symbol OPTION record omits `instrument_id`
- **THEN** the record SHALL be rejected with an `instrument_id` field error before persistence
- **THEN** different derivative contracts SHALL NOT silently merge under a shared symbol and null contract ID

#### Scenario: Different ordinary option contracts remain independent
- **WHEN** a BUY OPEN and a SELL OPEN have the same plain symbol but different strikes and distinct stable instrument IDs, without `order_id`
- **THEN** the import SHALL create two independent open contracts, not one closed trade

#### Scenario: Option contract fields derived from an OCC symbol
- **WHEN** an `OPTION` record omits `expiry`, `strike` and `option_type` and `symbol` is a valid OCC option symbol
- **THEN** the parser SHALL derive `expiry`, `strike` and `option_type` from the symbol

#### Scenario: Option record without contract information is rejected
- **WHEN** an `OPTION` record omits `expiry`, `strike` or `option_type` and `symbol` is not a valid OCC option symbol
- **THEN** the record SHALL be reported as invalid, naming the missing fields

#### Scenario: Option or future without multiplier is rejected
- **WHEN** a record has `asset_class` `OPTION` or `FUTURE` and no `multiplier`
- **THEN** the record SHALL be reported as invalid

#### Scenario: Lower-case enum values are accepted
- **WHEN** a record contains `side=buy` and `asset_class=equity`
- **THEN** the record SHALL be accepted as `BUY` / `EQUITY`

### Requirement: Mandatory execution identifier
Every record SHALL carry a non-empty `execution_id`. The identifier SHALL be unique within a file and stable across exports, because it is the idempotency key that prevents duplicates on re-import.

#### Scenario: Missing execution_id rejects the file
- **WHEN** any record has an empty or missing `execution_id`
- **THEN** the import SHALL fail and report the line number and the `execution_id` field

#### Scenario: Duplicate execution_id within one file rejects the file
- **WHEN** two records in the same file share the same `execution_id`
- **THEN** the import SHALL fail and report both line numbers

### Requirement: Timezone-aware execution timestamps
`executed_at` SHALL include an explicit UTC offset or `Z`. The system SHALL convert it to UTC. Timestamps without timezone information SHALL be rejected, never silently interpreted.

#### Scenario: Offset timestamp converted to UTC
- **WHEN** a record has `executed_at=2026-03-14T10:31:07-05:00`
- **THEN** the stored execution timestamp SHALL be `2026-03-14T15:31:07Z`

#### Scenario: Naive timestamp rejected
- **WHEN** a record has `executed_at=2026-03-14T10:31:07`
- **THEN** the record SHALL be reported as invalid with a message requiring a timezone offset

#### Scenario: UTC normalization exceeds the supported date range
- **WHEN** an offset timestamp cannot be represented after conversion to UTC
- **THEN** its `executed_at` field SHALL be reported as invalid alongside other row errors, without escaping as an unstructured processing exception

### Requirement: CSV encoding
The system SHALL accept the v1 format as CSV with UTF-8 encoding (an optional BOM SHALL be tolerated), comma delimiter, RFC 4180 quoting and a mandatory header row whose column names equal the v1 field names. Column order SHALL NOT matter. Empty cells SHALL be treated as absent values. Unknown columns SHALL be ignored and reported once as a warning. Corporate actions SHALL NOT be expressible in CSV.

#### Scenario: Columns in arbitrary order
- **WHEN** a CSV file contains all required columns in a different order than documented
- **THEN** the file SHALL be parsed successfully

#### Scenario: Missing required column
- **WHEN** the CSV header lacks a required column (e.g. `currency`)
- **THEN** the import SHALL fail with an error naming the missing column

#### Scenario: Unknown column produces a warning
- **WHEN** the CSV header contains a column `broker_note` that is not a v1 field
- **THEN** the import SHALL succeed and `ImportResult.warnings` SHALL mention the ignored column

#### Scenario: Excel-exported file with BOM
- **WHEN** the CSV file starts with a UTF-8 byte-order mark
- **THEN** the first header column SHALL be recognised correctly

### Requirement: JSON encoding
The system SHALL accept the v1 format as a JSON object with the keys `schema_version` (string, MUST be `"1"` or start with `"1."`), `executions` (array of v1 records) and optional `corporate_actions` (array of objects with `symbol`, `date` (`YYYY-MM-DD`), `ratio`, optional `old_instrument_id`, optional `new_instrument_id`). Numeric values MAY be JSON strings or JSON numbers; numbers SHALL be parsed without binary floating-point conversion.

#### Scenario: Valid JSON document
- **WHEN** a JSON file with `schema_version="1.0"` and a non-empty `executions` array is uploaded
- **THEN** every execution SHALL be imported

#### Scenario: Unsupported schema version
- **WHEN** a JSON file declares `schema_version="2.0"`
- **THEN** the import SHALL fail with an error naming the supported version

#### Scenario: Numeric price preserved exactly
- **WHEN** a JSON record contains `"price": 0.1`
- **THEN** the stored price SHALL equal `Decimal("0.1")` exactly

#### Scenario: Corporate actions imported from JSON
- **WHEN** a JSON file contains a `corporate_actions` entry for a 4:1 split
- **THEN** the split SHALL be passed to the import pipeline exactly like a corporate action parsed from a native broker file

#### Scenario: Unrepresentable split ratio rejected
- **WHEN** a corporate-action ratio exceeds precision 18 or scale 8, including a positive value that would round to zero
- **THEN** the document SHALL fail before persistence with a safe `corporate_actions[i]` / `ratio` error

### Requirement: Format detection by content
The parser SHALL detect the encoding from the file content: a payload whose first non-whitespace character (after an optional BOM) is `{` SHALL be parsed as JSON, otherwise as CSV. A payload starting with `<` SHALL be rejected with a message that XML is not supported for generic import.

#### Scenario: JSON with .json extension
- **WHEN** a file `trades.json` beginning with `{` is uploaded to a generic account
- **THEN** it SHALL be parsed as JSON

#### Scenario: XML rejected
- **WHEN** a file beginning with `<?xml` is uploaded to a generic account
- **THEN** the import SHALL fail with a message that generic import supports CSV and JSON only

### Requirement: All-or-nothing validation
The parser SHALL validate every record before anything is persisted. If one or more records are invalid, the import SHALL fail as a whole, no raw transaction SHALL be written, and the error response SHALL list every invalid record with its line number (CSV) or array index (JSON), the field name and a message. The error list SHALL NOT contain prices, quantities or P&L values.

#### Scenario: Two invalid rows in a 100-row file
- **WHEN** rows 12 and 57 of a CSV file are invalid and all other rows are valid
- **THEN** the import SHALL fail with HTTP 400
- **THEN** the response SHALL list both row 12 and row 57 with field and message
- **THEN** no raw transactions, executions or trades SHALL be created

#### Scenario: Empty file
- **WHEN** a CSV file contains only a header row, or a JSON file has an empty `executions` array
- **THEN** the import SHALL fail with a message that the file contains no executions

### Requirement: Mapping onto the unified import pipeline
Generic imports SHALL be processed by `ImportService.import_trades()` with `BrokerType.GENERIC`. The parser SHALL only implement `parse_raw()`, producing one `BrokerRawTransactionCreate` per record and the list of corporate actions. It SHALL NOT aggregate, deduplicate or persist data. The mapping SHALL be:

- `side` `BUY` → `direction=LONG`, `SELL` → `direction=SHORT`
- `open_close` `OPEN` → `"O"`, `CLOSE` → `"C"`, empty → `None`
- `instrument_id` → `conid` (empty → `None`)
- `pnl` = `realized_pnl` if present; otherwise `0` when `open_close=CLOSE`; otherwise `None`
- `raw_data["source_record"]` = the original record as read from the file, retained losslessly in a JSONB-safe representation
- The active top-level `raw_data` pipeline keys `brokerageOrderID` (= validated `order_id`) and `netCash` (derived) SHALL be written only from validated v1 fields when `order_id` is present. Unknown fields, including legacy `brokerageOrderID`, `netCash` and `openCloseIndicator`, SHALL remain inert inside `source_record` and SHALL NOT affect order grouping, cash, or mixed-execution flags.

#### Scenario: Closing execution without realized_pnl is treated as closing
- **WHEN** a record has `open_close=CLOSE`, no `order_id` and no `realized_pnl`
- **THEN** the raw transaction SHALL have `pnl=0` and the aggregator SHALL treat it as a closing leg

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
- **THEN** its trade grouping, direction, cash and logical mixed-execution flags SHALL equal the import without those extras
- **THEN** the original extras SHALL remain available inside `raw_data.source_record` only

### Requirement: Account-scoped idempotency
Generic files SHALL only be accepted by accounts of type `GENERIC`. The raw transaction identifier SHALL be `{user_id}:generic:{account_id}:{execution_id}`, so that the same `execution_id` in two different generic accounts never collides. Re-importing a record whose identifier already exists SHALL NOT modify the stored raw transaction.

#### Scenario: Re-importing the same file is a no-op
- **WHEN** a generic file is imported twice into the same account
- **THEN** the second import SHALL create no raw transactions, executions or trades

#### Scenario: Overlapping files
- **WHEN** file A (executions 1–50) and later file B (executions 40–90) are imported into the same account
- **THEN** executions 40–50 SHALL be skipped and only 51–90 SHALL be added

#### Scenario: Same execution_id in two generic accounts
- **WHEN** execution `1` is imported into generic account X and execution `1` into generic account Y of the same user
- **THEN** both SHALL be stored as distinct raw transactions

#### Scenario: Protected trade in another account does not interfere
- **WHEN** account A has a manually edited/protected trade and the same ordinary fill is imported into generic account B in the same portfolio
- **THEN** B SHALL receive its independent raw transaction, linked logical execution and trade
- **THEN** A's protected trade SHALL remain unchanged

#### Scenario: Different null-ID instruments at one timestamp remain independent
- **WHEN** a generic account contains a protected trade and another fill at the same timestamp has a different symbol or asset class but no instrument ID
- **THEN** protection membership and existing-trade fallback matching SHALL distinguish their complete business identities
- **THEN** the independent trade SHALL be created and linked without reconciling into or modifying the protected trade

#### Scenario: Generic file rejected for broker accounts
- **WHEN** a `.csv` file is uploaded to an IBKR, Schwab or manual account
- **THEN** the upload SHALL be rejected with HTTP 422

### Requirement: Schema and template downloads
The API SHALL expose, without requiring portfolio ownership beyond authentication, a JSON Schema document for the v1 JSON format generated from the Pydantic model, and a CSV template containing the header row and example rows (equity round trip, option spread with `order_id`). The import form SHALL link to both downloads when a generic account is selected.

#### Scenario: Download JSON Schema
- **WHEN** an authenticated user requests the generic JSON Schema
- **THEN** the response SHALL be `application/schema+json` describing every v1 field, its type and whether it is required
- **THEN** the input schema SHALL accept the advertised case-insensitive enum spellings and numeric multiplier strings as well as numbers, and describe conditional derivative/order requirements

#### Scenario: Download CSV template
- **WHEN** an authenticated user requests the CSV template
- **THEN** the response SHALL be a `text/csv` attachment whose header equals the v1 field list and which itself imports without errors into an empty generic account

### Requirement: User documentation
The format SHALL be documented in `docs/generic-import-format.md` (field reference, CSV and JSON examples, validation rules, idempotency and correction guidance, converting a broker export, splitting a position flip into a close and an open row) and in the in-app help under a section "Importing from other brokers". The import form SHALL show a format hint for generic accounts that links to the help entry.

#### Scenario: Help entry available
- **WHEN** the user opens `/help`
- **THEN** a section "Importing from other brokers" SHALL describe the generic format and link to the template downloads

#### Scenario: Import form hint
- **WHEN** a generic account is selected in the import form
- **THEN** the form SHALL accept `.csv` and `.json` files and show the format hint with links to the help entry and templates

### Requirement: Native-versus-generic parity
The repository SHALL contain generic CSV and JSON fixtures produced from the existing IBKR and Schwab test fixtures by a deterministic conversion tool. Importing a generic fixture into a generic account SHALL produce the same trades as importing the native fixture into a broker account.

#### Scenario: IBKR fixture parity
- **WHEN** an IBKR fixture is imported natively into account A and its generic conversion into generic account B
- **THEN** both accounts SHALL contain the same number of trades
- **THEN** each trade pair SHALL match on symbol, asset class, direction, quantity, entry price, exit price, commission, opened_at, closed_at, P&L and combo grouping

#### Scenario: Schwab fixture parity
- **WHEN** a Schwab fixture is imported natively and its generic conversion into a generic account
- **THEN** the resulting trades SHALL match as in the IBKR parity scenario

#### Scenario: CSV and JSON fixtures are equivalent
- **WHEN** the CSV and JSON generic versions of the same fixture are imported into two generic accounts
- **THEN** the resulting trades SHALL be identical

#### Scenario: Fixtures stay in sync with the converter
- **WHEN** the conversion tool is re-run on the native fixtures
- **THEN** its output SHALL be byte-identical to the committed generic fixtures
