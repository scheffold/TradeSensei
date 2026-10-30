# API Usage Notes

## Clear All Trades (Account-Scoped)

Use this endpoint to reset an account's imported and manually edited trade data
without deleting the account itself.

- Method: `DELETE`
- Path: `/api/v1/portfolios/{portfolio_id}/accounts/{account_id}/trades`
- Query param: `acknowledged=true` (required)

Behavior:

- Deletes all `trades`, `logical_executions`, and `broker_raw_transactions` for
  the account.
- Preserves the account row and account settings.
- Returns deletion counts in `ResponseEnvelope[ClearTradesResult]`.
- Returns `400` when `acknowledged` is missing/false.
- Returns `404` when the account is not owned by the authenticated user.

Example:

```bash
curl -X DELETE \
  "http://localhost:8000/api/v1/portfolios/<portfolio_id>/accounts/<account_id>/trades?acknowledged=true" \
  -H "Authorization: Bearer <access_token>"
```

## Import UI Date Range (IBKR + Schwab)

The frontend sync UI now uses one shared date-range control for both IBKR and
Schwab.

- IBKR sync requests send `from_date=YYYYMMDD`.
- Schwab sync requests send `days=<N>`.

The control enforces broker-specific max window limits from a capability
descriptor and removes broker-specific component branching.

## IBKR Flex API Import Pipeline

The IBKR Flex API endpoint and IBKR XML file upload now use the exact same
import pipeline.

- `POST /api/v1/portfolios/{portfolio_id}/imports/flex` fetches Flex XML,
  then delegates to `ImportService.import_trades()` with `BrokerType.IBKR`.
- `POST /api/v1/portfolios/{portfolio_id}/imports/upload` for IBKR XML uses
  the same parser and raw-transaction execution pipeline.

As a result, both ingestion paths are behaviorally identical:

- both persist `BrokerRawTransaction` rows,
- both create and link `LogicalExecution` rows,
- both build trades from the same projection logic,
- both return the same import counters/protection fields.

## Generic execution file import (other brokers)

Generic CSV and JSON files use the same raw-transaction import pipeline as the
native brokers. See the [v1 format reference](generic-import-format.md) for all
fields, examples, validation rules and converter guidance.

All requests below require a bearer access token. Set `ACCESS_TOKEN` to the
`access_token` returned by login. The UUIDs show the expected format; use your
owned portfolio ID and the account ID returned by account creation.

Create an account with `type="generic"` using
`POST /api/v1/portfolios/{portfolio_id}/accounts`:

```bash
PORTFOLIO_ID=3fa85f64-5717-4562-b3fc-2c963f66afa6
curl -X POST \
  "http://localhost:8000/api/v1/portfolios/$PORTFOLIO_ID/accounts" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Other broker","type":"generic"}'
```

The account inherits its portfolio's base currency. Generic accounts reject
broker credentials and do not support API connections, connection tests or sync.
Prefer a new account when moving between native and generic imports: type
changes to/from generic return HTTP 422 while any raw/logical/trade history
remains, including deleted/suppressed records, even through `manual`.

To deliberately reuse an account, **Clear All Trades** must remove every history
layer before changing type; deleting visible trades is insufficient. Stored
credentials also block switching to generic. Account updates and imports are
serialized for that account; [clearing trades](#clear-all-trades-account-scoped)
is destructive, so keep your source exports first.

### Upload fills

- Method and path: `POST /api/v1/portfolios/{portfolio_id}/imports/upload`
- Auth: bearer token; the account must belong to the owned path portfolio.
- Request: multipart fields `account_id` and `file`, with one execution per
  row/object. No `broker` field is needed: the account type selects the parser.

Upload the equity round-trip example from the format reference:

```bash
ACCOUNT_ID=3fa85f64-5717-4562-b3fc-2c963f66afa7
curl -X POST \
  "http://localhost:8000/api/v1/portfolios/$PORTFOLIO_ID/imports/upload" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "account_id=$ACCOUNT_ID" \
  -F "file=@executions.csv;type=text/csv"
```

For JSON, change the last argument to
`-F "file=@executions.json;type=application/json"`. The HTTP 200 response is
`ResponseEnvelope[ImportResult]`. For the two equity fills in an empty account,
a response excerpt is:

```json
{
  "data": {
    "broker": "generic",
    "filename": "executions.csv",
    "trades_parsed": 1,
    "trades_created": 1,
    "trades_skipped": 0,
    "raw_transactions_created": 2,
    "raw_suppressed_skipped": 0,
    "raw_deleted_skipped": 0,
    "trades_protected_reconciled": 0,
    "trades_protected_skipped": 0,
    "protected_positions_closed": 0,
    "unlinked_executions": 0,
    "warnings": []
  },
  "meta": {}
}
```

`raw_transactions_created` counts new fills, while `trades_created` counts
projected trades. Protection counters explain preserved or unlinked executions;
`activity_log` (omitted above) contains routine informational outcomes, not
warnings. Generic accounts accept `.csv` and `.json` only; the maximum file size
is 10 MB.

Every fill needs a stable `execution_id`. Its stored raw ID is
`{user_id}:generic:{account_id}:{execution_id}`. Re-imports and overlapping files
skip existing IDs without updating them; the same execution ID in another
generic account has a separate raw ID.

- Require `instrument_id` for FUTURE, plain-symbol OPTION and **any `order_id`**,
  even a single OCC-symbol leg. Without order metadata, ordinary EQUITY/FOREX/CRYPTO
  and valid OCC-symbol options may omit it. Keep ID presence/values and symbol
  spelling consistent across fills and exports; explicit fields must match OCC symbols.
- Every order-linked row needs explicit `open_close`. Keep an order ID stable
  across exports of the same order, but use different IDs for opening/closing
  orders. Markers must agree within the file and with target-account raw history.
- Every order-linked CLOSE needs `realized_pnl`, including genuine `0` for
  breakeven. OPEN with non-null P&L, including zero, is invalid. Only ordinary
  CLOSE fills **without order metadata** retain the zero-marker fallback.
- Original records and arbitrary unknown fields remain inert under
  `raw_data.source_record`. Legacy `brokerageOrderID`, `netCash` and
  `openCloseIndicator` extras cannot supply active order/cash/flip behavior.
- Account-scoped raw IDs do not alone isolate protection/fallback matching:
  that also uses account, symbol, asset class, contract ID and opening timestamp,
  with execution anchors taking precedence where available.

Execution decimals and split ratios fit `Numeric(18,8)`; a positive subscale
ratio is rejected rather than rounded to zero. Timestamps need `Z` or an offset
and must remain representable after UTC conversion. The reference covers these
limits and the conditional fields; conditional fields are not universally
mandatory CSV headers.

### Validation and error responses

Row-validation failures return HTTP 400, reject the entire file before
persistence, and use a structured detail object rather than the string detail
used by other import errors:

```json
{
  "detail": {
    "message": "Generic import validation failed.",
    "errors": [
      {
        "location": 2,
        "field": "executed_at",
        "message": "executed_at requires an explicit timezone offset or Z."
      }
    ]
  }
}
```

Locations are physical CSV line numbers (including the header) or JSON paths
such as `executions[0]`. Duplicate execution IDs report every affected location.
Responses include at most 200 row errors plus a marker for any remaining errors.
Messages do not echo execution values. Other broker error responses are unchanged.

| Status | Meaning and example body |
|---|---|
| `401` | Missing token: `{"detail":"Not authenticated"}`. Invalid/expired token: `{"detail":"Could not validate credentials."}`. |
| `404` | Missing or unowned portfolio/account, or account outside the path portfolio; for example `{"detail":"Portfolio not found."}`. |
| `413` | `{"detail":"File exceeds the maximum allowed size of 10 MB."}`. |
| `422` | Invalid multipart fields or file/account mismatch. For an XML upload to the example account: `{"detail":"Account 'Other broker' (GENERIC) only accepts .csv, .json files. Received: .xml"}`. |

CSV uploads to IBKR/Schwab and all file uploads to manual accounts are rejected
with HTTP 422. A generic JSON document is not a native Schwab export; selecting
Schwab does not make the generic format compatible with it.

### Schema and template downloads

Both endpoints require bearer authentication but are **not portfolio-scoped**.
Their bodies are raw documents, not response envelopes:

| Endpoint | Content type | Body |
|---|---|---|
| `GET /api/v1/imports/generic/schema` | `application/schema+json` | JSON Schema generated from the v1 Pydantic document. |
| `GET /api/v1/imports/generic/template.csv` | `text/csv` attachment | All v1 field headers, an equity round trip and an option spread with explicit contract/order IDs. |

```bash
curl "http://localhost:8000/api/v1/imports/generic/schema" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -o generic-import-schema.json

curl "http://localhost:8000/api/v1/imports/generic/template.csv" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -o generic-import-template.csv
```

The schema response describes the input document, not imported trades. This
excerpt omits `$defs`, where execution fields, conditional requirements and
corporate-action definitions appear:

```json
{
  "title": "GenericImportDocumentV1",
  "type": "object",
  "required": ["schema_version", "executions"],
  "properties": {
    "schema_version": {"type": "string", "pattern": "^1(?:\\..*)?$"},
    "executions": {
      "type": "array",
      "minItems": 1,
      "items": {"$ref": "#/$defs/GenericExecutionV1"}
    }
  }
}
```

The template's first two example rows follow the original 17-field order:

```csv
execution_id,executed_at,symbol,asset_class,side,quantity,price,currency,commission,open_close,multiplier,instrument_id,expiry,strike,option_type,order_id,realized_pnl
example-equity-open,2026-03-16T14:30:00Z,AAPL,EQUITY,BUY,10,150.00,USD,1.00,OPEN,,,,,,,
example-equity-close,2026-03-16T15:30:00Z,AAPL,EQUITY,SELL,10,152.00,USD,1.00,CLOSE,,,,,,,
```

The full template also includes an opening option spread with distinct contract
IDs and a shared order ID. Both downloads return HTTP 200 attachments with
`Content-Disposition` filenames `generic-executions-v1.schema.json` and
`generic-executions-v1.csv`, respectively. Missing/invalid tokens return the
same HTTP 401 JSON details shown above, with `WWW-Authenticate: Bearer`.

Neither endpoint takes account IDs, portfolio IDs or a request body, so there
are no endpoint-specific 404/422 cases. On the Import and Help pages, use the
authenticated download controls; opening a plain API URL does not attach a
bearer token.

### Python client downloads

The repository ships an OpenAPI-generated TypeScript client, not a prebuilt
Python SDK. Python consumers can generate a client from `/openapi.json` using
`openapi-python-client`. For a package named `tradesensei_client`, save this
generator configuration as `python-client.yaml` in your client project:

```yaml
package_name_override: tradesensei_client
```

With that generator available in your Python environment:

```bash
openapi-python-client generate --url http://localhost:8000/openapi.json \
  --config python-client.yaml --output-path ./python-client
```

Run the following with the generated package on your Python path and
`ACCESS_TOKEN` set from login. `sync_detailed()` preserves the raw attachment
bytes, regardless of whether the generator also parses a response model:

```python
import os
from pathlib import Path

from tradesensei_client import AuthenticatedClient
from tradesensei_client.api.imports import (
    get_generic_import_schema_api_v1_imports_generic_schema_get as get_schema,
    get_generic_import_template_api_v1_imports_generic_template_csv_get as get_template,
)

with AuthenticatedClient(
    base_url="http://localhost:8000", token=os.environ["ACCESS_TOKEN"]
) as client:
    for endpoint, filename in (
        (get_schema, "generic-import-schema.json"),
        (get_template, "generic-import-template.csv"),
    ):
        response = endpoint.sync_detailed(client=client)
        if response.status_code != 200:
            raise RuntimeError(f"Download failed: HTTP {response.status_code.value}")
        Path(filename).write_bytes(response.content)
```

## Execution Leg Shape (Options)

For option trades (where `asset_class` is `"OPTION"` or when part of a COMBO trade), `LogicalExecution` objects in API responses (e.g. `GET /api/v1/trades/{id}`) will include strongly-typed option instrument fields:

- `strike` (string/decimal): Option strike price (e.g., `"712.00000000"`).
- `expiry` (string): Option expiration date in `YYYY-MM-DD` format.
- `option_type` (string): `"C"` for Call, `"P"` for Put.

For non-option legs, these fields will be `null`. The raw OCC string or broker-specific symbol remains accessible in the `symbol` field.


## Link Manual Execution to Broker Transaction

Use this endpoint to link an unlinked manual execution leg (`origin="manual"`) to a raw broker transaction (`BrokerRawTransaction`).

- Method: `POST`
- Path: `/api/v1/portfolios/{portfolio_id}/executions/{execution_id}/link-broker-transaction`
- Auth required: `Bearer <token>`

Request body (`LinkBrokerTransactionRequest`):

```json
{
  "raw_transaction_id": "user:ibkr:3820422546"
}
```

Behavior:

- Atomically links the manual execution leg to the specified raw broker transaction (`source_id = raw_transaction_id`) and sets `suppressed = true` on the broker raw transaction.
- Triggers background trade re-aggregation for affected trades, auto-clearing any duplicate conflict flags.
- Deleting a linked manual execution automatically un-suppresses (`suppressed = false`) the associated raw transaction in the same transaction.

Example request:

```bash
curl -X POST \
  "http://localhost:8000/api/v1/portfolios/3fa85f64-5717-4562-b3fc-2c963f66afa6/executions/3fa85f64-5717-4562-b3fc-2c963f66afa7/link-broker-transaction" \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{ "raw_transaction_id": "user:ibkr:3820422546" }'
```

Example response (`ResponseEnvelope[LinkBrokerTransactionResponse]`):

```json
{
  "data": {
    "execution": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa7",
      "trade_id": "3fa85f64-5717-4562-b3fc-2c963f66afa8",
      "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "account_id": "3fa85f64-5717-4562-b3fc-2c963f66afa9",
      "symbol": "AAPL",
      "direction": "LONG",
      "quantity": "100.00000000",
      "price": "175.50000000",
      "commission": "1.00000000",
      "executed_at": "2026-04-15T14:30:00Z",
      "asset_class": "EQUITY",
      "multiplier": 1,
      "origin": "manual",
      "deleted": false,
      "user_modified": false,
      "source_id": "user:ibkr:3820422546",
      "parent_id": null,
      "created_at": "2026-04-15T14:30:00Z",
      "updated_at": "2026-04-15T14:35:00Z"
    },
    "message": "Execution linked to broker transaction."
  },
  "error": null
}
```

Error responses:

- `404 Not Found`: Execution or broker raw transaction does not exist or is not owned by the user.
- `409 Conflict`: Execution `origin` is not `"manual"`, or the raw broker transaction is already linked to another execution.
- `422 Unprocessable Content`: Invalid request body (e.g. missing `raw_transaction_id`).



## Trade Conflict Detection (has_conflict)

The `TradeRead` schema returned by trade endpoints (e.g., `GET /api/v1/portfolios/{portfolio_id}/trades/{id}`) includes a derived boolean field:

- `has_conflict` (boolean): Set to `true` when a trade contains an unlinked manual execution leg (`origin="manual"`, `source_id=null`) that duplicates an imported leg (`origin="imported"`) matching on symbol, execution time (to the second), direction, quantity, and price.

Behavior:

- `has_conflict` is a read-only derived flag calculated during trade aggregation. It cannot be set directly via `PATCH /trades/{id}`.
- Auto-clears to `false` when the duplicate leg is deleted or when the manual leg is linked to a raw broker transaction using `POST /executions/{id}/link-broker-transaction`.

## Protected Trade Unlinked Execution Signal

The `TradeRead` schema now also includes:

- `has_unlinked_executions` (boolean): `true` when at least one broker execution was left unlinked because a protected trade (`is_manually_edited=true`) was isolated from rebuild updates during import.

Behavior:

- This field is additive and defaults to `false`.
- The flag is set during import when protection blocks auto-linking and is cleared by a later re-aggregation once no outstanding unlinked broker execution remains for the trade's contract set.

## Trade List Filter: has_unlinked_executions

`GET /api/v1/portfolios/{portfolio_id}/trades` accepts an optional query parameter:

- `has_unlinked_executions=true` → return only flagged trades.

When omitted, list behavior is unchanged from prior versions.

Example:

```bash
curl -X GET \
  "http://localhost:8000/api/v1/portfolios/<portfolio_id>/trades?has_unlinked_executions=true" \
  -H "Authorization: Bearer <access_token>"
```
