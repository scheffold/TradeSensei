# Generic execution import format (v1)

Use this format to import fills from a broker without native support, or to
record executions in a spreadsheet. Each row or JSON object represents **one
execution (fill), not an entire trade**. Trade Sensei matches those executions
through the same import pipeline used for IBKR and Schwab.

Create an **Other broker (CSV/JSON)** account in the target portfolio, then select
it on the Import page. Generic accounts accept `.csv` and `.json` files only;
they do not store broker credentials or support API sync. IBKR, Schwab and manual
accounts cannot accept generic imports. Account currency comes from its portfolio.

## Reference

### Execution fields

CSV column names and JSON field names are case-sensitive. Enumerated values
(`asset_class`, `side`, `open_close`, `option_type`) are case-insensitive.
Surrounding string whitespace is stripped for validation. Empty cells and null
optional values are treated as absent.

| Field | Required | Type, default and rules |
|---|---|---|
| `execution_id` | Yes | Stable, non-empty string, at most 100 characters. Unique within a file. |
| `executed_at` | Yes | ISO 8601 timestamp with `Z` or an explicit UTC offset. Must remain representable after conversion to UTC. |
| `symbol` | Yes | Non-empty instrument symbol, at most 30 characters. An OCC option symbol can supply missing contract fields. |
| `asset_class` | Yes | `EQUITY`, `OPTION`, `FUTURE`, `FOREX` or `CRYPTO`. |
| `side` | Yes | `BUY` or `SELL`; not the direction of the whole position. |
| `quantity` | Yes | Decimal greater than zero. Always an absolute fill quantity. |
| `price` | Yes | Decimal greater than or equal to zero, per unit **without** the multiplier. |
| `currency` | Yes | Three uppercase ISO 4217 letters, for example `USD` or `EUR`. |
| `commission` | No | Non-negative decimal; defaults to `0`. Sum all execution fees as an absolute value. |
| `open_close` | Whenever `order_id` is present | `OPEN` or `CLOSE`. Without order metadata, empty means unspecified; mark pre-period closes explicitly. |
| `multiplier` | For `OPTION` and `FUTURE` | Integer at least `1`, explicitly supplied for these asset classes. Defaults to `1` otherwise. |
| `instrument_id` | For `FUTURE`, plain-symbol `OPTION`, and any `order_id` | Stable, non-empty contract identifier, at most 50 characters. Distinct contracts need distinct IDs, reused on opening/closing fills and across exports. |
| `expiry` | For `OPTION` unless derivable from OCC | `YYYY-MM-DD`. Optional for `FUTURE`. |
| `strike` | For `OPTION` unless derivable from OCC | Decimal greater than zero. |
| `option_type` | For `OPTION` unless derivable from OCC | `C` (call) or `P` (put). |
| `order_id` | No | Identifies one multi-leg order. Stable across exports of that order; opening and closing orders use different IDs. |
| `realized_pnl` | For `CLOSE` with `order_id` | Decimal broker-reported realized P&L of a closing execution, including explicit `0` for a genuine breakeven close. Absent/null for `OPEN`, even when the reported value is zero. |

Execution decimals fit `Numeric(18,8)`: at most 10 integer digits and 8 fractional places.
Use `.` as the decimal separator, without thousands separators. JSON numbers
and decimal strings are accepted without a binary floating-point conversion.
Non-finite values, negative quantities, negative prices and negative fees are
invalid; quantity and strike must also be greater than zero.

A naive timestamp such as `2026-03-14T10:31:07` is invalid; use, for example,
`2026-03-14T10:31:07-05:00`, which converts to `2026-03-14T15:31:07Z`.
Conversion must stay within years 0001–9999: `0001-01-01T00:00:00+01:00`, for
example, falls outside that range in UTC and produces an `executed_at` error.

### Contract identity

Every `FUTURE` fill and every `OPTION` fill using a plain symbol such as `QQQ`
requires `instrument_id`. Expiry, strike and option type alone do not identify
separate contracts in FIFO matching. Use a distinct stable ID for each contract,
for example `qqq-20260619-c-500` and `qqq-20260619-c-510`.

Without `order_id`, ordinary `EQUITY`, `FOREX` and `CRYPTO` fills may omit
`instrument_id`. A standalone option with a valid contract-specific OCC symbol,
such as `AAPL  260619C00200000`, may also omit it. Missing option fields are
derived from the OCC symbol; explicitly supplied fields must agree with it.

**Any `order_id` requires an instrument ID and an explicit OPEN/CLOSE marker**,
even for one leg uploaded separately or an OCC-symbol option. Keep symbol
spelling and ID presence/values consistent across opening/closing fills and
later exports: do not switch between a plain symbol and an OCC symbol, or start
adding IDs to previously ID-free fills for the same instrument. V1 does not
invent contract IDs or change the FIFO/combo algorithms to resolve ambiguity.

### CSV encoding

- UTF-8, optionally with a byte-order mark (BOM), and comma-separated columns.
- A header row is mandatory. Include all eight required execution columns.
- Column order does not matter. Include conditional columns when a row needs
  them; they are not mandatory headers for an ordinary equity file.
- Use normal CSV quoting for commas, quotes and newlines inside cells.
- Unknown columns are ignored for import behavior and produce a warning once
  per column. Their source values are retained as described below.
- A header-only file is invalid. Corporate actions are JSON-only.

### JSON encoding

The document is an object containing:

| Key | Required | Rules |
|---|---|---|
| `schema_version` | Yes | String `"1"` or any string starting with `"1."`, such as `"1.0"`. Numeric versions and other major versions are rejected. |
| `executions` | Yes | Non-empty array of execution records. |
| `corporate_actions` | No | Array of stock splits; defaults to an empty array. |

A corporate-action object contains `symbol`, `date` (`YYYY-MM-DD`), a positive
decimal `ratio` (shares after the split divided by shares before), and optional
`old_instrument_id` and `new_instrument_id` strings (at most 50 characters).
For example, `ratio="4"` means a 4:1 forward split and `ratio="0.05"` a 1:20
reverse split. These actions enter the existing corporate-action pipeline.

Ratios must also fit `Numeric(18,8)`: the smallest positive ratio is
`0.00000001` and the largest is `9999999999.99999999`. A ratio such as
`0.000000001` is rejected, not rounded to zero. Invalid ratios produce safe
`corporate_actions[i]` / `ratio` errors before persistence.

### Source metadata is inert

The original execution record, including unknown fields, is retained under
`raw_data.source_record`. JSON decimal literals are represented losslessly as
JSON-safe decimal strings. Unknown fields do not influence import behavior.

In particular, copying native `brokerageOrderID`, `netCash` or
`openCloseIndicator` keys into a generic record does not group orders, override
cash or mark a position flip. Only validated v1 fields populate active pipeline
metadata: `order_id` supplies the order key, and quantity, price, multiplier and
commission determine its cash. Use the documented fields, not native control keys.

### Detection and validation errors

Encoding is detected from content, not the filename. After an optional BOM and
leading whitespace, `{` starts JSON; other content is read as CSV. Content
starting with `<` is rejected: XML is not supported. The upload extension must
still be `.csv` or `.json`. The maximum file size is 10 MB.

The whole file is validated before any raw transactions, logical executions
or trades are saved. If any row is invalid, the upload returns HTTP 400 with
`detail.message` and `detail.errors`. Each error has:

- `location`: the physical CSV line number, including the header, or a JSON
  index such as `executions[0]` or `corporate_actions[1]`;
- `field`: the invalid field name;
- `message`: a validation explanation without the supplied value.

Duplicate `execution_id` values report every affected location. Large error
responses include the first 200 row errors, plus a summary entry when more errors
exist.
Fix all reported rows and submit the file again. A format/account mismatch
returns HTTP 422 instead of a row-validation error.

## How to import a first round trip

1. Create an **Other broker (CSV/JSON)** account in your portfolio.
2. Save this as `executions.csv`, or use the JSON equivalent below.
3. Select that account on the Import page and upload the file. Check the import
   summary and your trade list; the two fills form one closed equity trade.

```csv
execution_id,executed_at,symbol,asset_class,side,quantity,price,currency,commission,open_close
equity-open-1,2026-03-14T14:30:00Z,AAPL,EQUITY,BUY,10,200,USD,1,OPEN
equity-close-1,2026-03-14T15:30:00Z,AAPL,EQUITY,SELL,10,205,USD,1,CLOSE
```

```json
{
  "schema_version": "1.0",
  "executions": [
    {
      "execution_id": "equity-open-1",
      "executed_at": "2026-03-14T14:30:00Z",
      "symbol": "AAPL",
      "asset_class": "EQUITY",
      "side": "BUY",
      "quantity": "10",
      "price": "200",
      "currency": "USD",
      "commission": "1",
      "open_close": "OPEN"
    },
    {
      "execution_id": "equity-close-1",
      "executed_at": "2026-03-14T15:30:00Z",
      "symbol": "AAPL",
      "asset_class": "EQUITY",
      "side": "SELL",
      "quantity": "10",
      "price": "205",
      "currency": "USD",
      "commission": "1",
      "open_close": "CLOSE"
    }
  ]
}
```

The Import page also offers authenticated downloads:

- **JSON Schema:** `GET /api/v1/imports/generic/schema`
- **CSV template:** `GET /api/v1/imports/generic/template.csv`

The schema is generated from the v1 Pydantic document and describes conditional
requirements and accepted input casing. The template keeps all 17 fields in the
reference order and includes an equity round trip and an option spread.
Both endpoints require login, but do not require selecting a portfolio.

Schema validation checks the input shape and conditional fields, but does not
replace the importer. Normalized decimal precision/scale and exponent bounds,
UTC representability, OCC-field agreement, duplicate IDs and order-history
consistency are checked by the importer before persistence.

On the Import and Help pages, use the **Download JSON Schema** and
**Download CSV template** controls; they authenticate the requests for you.
A plain API URL does not supply your login token. See
[API usage](api-usage.md#schema-and-template-downloads) for curl and Python examples.

## How to mark closing executions

Use `open_close=CLOSE` for every closing fill, especially when its opening fill
predates the exported period. Without that marker or `realized_pnl`, an unmatched
SELL can be interpreted as a new short position instead of an orphan close.

Without `order_id`, `realized_pnl` is passed through when supplied; otherwise,
`CLOSE` maps to the pipeline's zero-P&L closing marker. This marker identifies
an ordinary FIFO close; it does **not** mean the resulting trade's calculated
P&L must be zero. An unspecified position effect without `realized_pnl` maps to
an opening/unknown execution.

With **any `order_id`**, supply `open_close` and `instrument_id` on every row.
Every order-linked `CLOSE` also needs broker-reported `realized_pnl`, even when
only one contract appears in this file. Combo P&L sums those reported values;
omission cannot stand in for zero. Explicit `0` is valid for a genuine breakeven
close.

An explicit `OPEN` must leave `realized_pnl` absent, null or blank in CSV.
`OPEN` plus a non-null P&L, **including `0`**, is rejected because non-null P&L
identifies a closing execution in the existing pipeline.

### Position flips

Split a fill that crosses zero into two execution records. For example, selling
15 shares when you hold 10 long becomes:

1. A SELL of 10 with `open_close=CLOSE` and its own execution ID.
2. A SELL of 5 with `open_close=OPEN` and a second execution ID.

Keep the timestamp and per-unit price on both rows. Allocate commission between
the portions without duplicating fees, and put broker-reported realized P&L only
on the close. If you include order metadata, each row needs an instrument ID, the
close needs its reported P&L, and the CLOSE and OPEN portions need different order
IDs. V1 has no combined position-flip marker.

## How to import multi-leg orders

Give every distinct contract a stable `instrument_id`. Use the same ID when
closing that contract and across later exports. Repeated fills of the same
contract can share its instrument ID; they still need distinct execution IDs.

Legs intended to form one combo order share `order_id`, the execution timestamp
and a consistent `open_close` marker. Keep an order's ID stable across its
exports; use a different ID for the closing order. Marker consistency is checked
both within the file and against already imported generic raw transactions in
the target account before anything is inserted.

```csv
execution_id,executed_at,symbol,asset_class,side,quantity,price,currency,commission,open_close,multiplier,instrument_id,expiry,strike,option_type,order_id,realized_pnl
spread-leg-1,2026-03-14T14:30:00Z,QQQ,OPTION,BUY,1,10,USD,1,OPEN,100,qqq-20260619-c-500,2026-06-19,500,C,spread-open-1,
spread-leg-2,2026-03-14T14:30:00Z,QQQ,OPTION,SELL,1,7,USD,1,OPEN,100,qqq-20260619-c-510,2026-06-19,510,C,spread-open-1,
spread-leg-3,2026-03-14T15:30:00Z,QQQ,OPTION,SELL,1,12,USD,1,CLOSE,100,qqq-20260619-c-500,2026-06-19,500,C,spread-close-1,198
spread-leg-4,2026-03-14T15:30:00Z,QQQ,OPTION,BUY,1,8,USD,1,CLOSE,100,qqq-20260619-c-510,2026-06-19,510,C,spread-close-1,-102
```

The closing rows report `198` and `-102`, totaling `96` after the example's fees.
Opening rows leave `realized_pnl` blank. Do not copy a zero marker from ordinary
FIFO examples into combo closes unless the broker actually reports breakeven.

Different contracts sharing an order ID cannot reuse one instrument ID.
Every order-linked row needs its ID and marker even when you upload the legs in
separate files, and every closing leg needs its reported P&L. Keep the same
contract IDs and symbol spelling across files; only ordinary non-derivatives and
standalone valid OCC-symbol options **without order metadata** may omit IDs.

When `order_id` is present, the generic adapter supplies the existing pipeline's
order metadata and derives cash as:

```text
notional = quantity × price × multiplier
net cash = +notional − commission  (SELL)
net cash = −notional − commission  (BUY)
```

### Known parity limitation: broker cash overrides

V1 has no field for an arbitrary broker-reported net-cash override. The native
`combo_positions.xml` test fixture illustrates why this matters: execution
`900000011` is a BUY-to-close of one contract at `63.03`, multiplier `100`, and
commission `0.69028`, but its native `netCash` is positive `3153.0`. Generic v1
correctly derives `-6303.69028` from those execution fields instead.

The native fixture and parser are unchanged. The parity tests explicitly check
this one raw-metadata exception and require every specified trade/P&L field and
combo leg grouping to remain equal. All other order cash values are compared
to two decimal places. Closing cash does not determine combo direction;
opening cash does. Do not alter prices, quantities or fees just to imitate an
inconsistent cash override.

## Re-imports and corrections

`execution_id` is the duplicate-prevention key (idempotency key). It must stay
stable across exports. Do not generate IDs from row numbers that change when
an export grows, and do not invent a new ID just to correct an existing fill.

IDs are scoped to the user **and generic account**. Re-importing the same file
into the same account creates no additional rows. Overlapping files add only
previously unseen IDs. The same execution ID in two different generic accounts
is stored separately, so keep each broker/source in its own account.

Raw-ID scoping prevents duplicate-key collisions; it is not the whole matching
identity. Protection and existing-trade fallback matching also distinguish the
account, symbol, asset class, instrument ID and opening timestamp, with execution
anchors taking precedence where available. A protected trade must not capture
another account's fills or a different instrument with no ID at the same time.

Re-importing an existing ID does not update the stored raw execution, even if
you changed its values. Correct an imported execution in the execution editor,
or use the account's **Clear All Trades** action and re-import a corrected
complete history. Clear actions are destructive; keep your source exports and
review the confirmation before proceeding.

### Changing an account type

Prefer creating a **new account** when moving between a native broker and generic
imports. Changing type to or from `generic` is blocked while any raw transactions,
logical executions or trades remain, including suppressed raw records and deleted
execution/trade history. Switching through `manual` does not bypass this guard.

If you deliberately reuse an account, **Clear All Trades** must remove all three
history layers before the type change; deleting visible trades alone is not
enough. Keep backups before clearing, and note that an empty account still cannot
become generic while it stores broker credentials. The history check and type
update are serialized against imports for the same account.

## How to convert a broker export

Map each original fill to the reference fields above and preserve the broker's
stable execution identifier. Use one record per fill, not per position summary.
Check signs, absolute commissions, UTC offsets and contract multipliers before
uploading. Do not convert through binary floating-point values.

[`tools/convert_to_generic.py`](../tools/convert_to_generic.py) is a runnable
reference for native IBKR XML and wrapped Schwab JSON exports. From the repository
root, with the backend dependencies available through `uv`:

```bash
uv run --project backend python tools/convert_to_generic.py \
  tests/test_data/ibkr/combo_positions.xml --broker ibkr \
  --output-stem tests/test_data/generic/ibkr_combo_positions
```

For Schwab, use `--broker schwab` and its native JSON file. The tool calls the
native parser's `parse_raw()`, sorts by UTC timestamp and ID, and writes both
`.csv` and `.json` files with deterministic decimal strings. It never aggregates
or writes to a database. The two-format converter rejects files containing
corporate actions because CSV cannot represent them; encode those actions in
generic JSON instead. The reference CSV/JSON examples live in
[`tests/test_data/generic/`](../tests/test_data/generic/).

For troubleshooting, start with the row locations shown by the Import page,
confirm the selected account is **Other broker (CSV/JSON)**, and compare your
headers or JSON keys with the reference. In-app guidance is available at
`/help#generic-import-format`.
