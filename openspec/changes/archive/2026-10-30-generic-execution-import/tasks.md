## 1. Account type

- [x] 1.1 Add `AccountType.GENERIC = "generic"` in `backend/app/models/account.py` (docstring included)
- [x] 1.2 Alembic migration: `ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'generic'` inside `autocommit_block()`; downgrade no-op with comment (use `db-migration` skill)
- [x] 1.3 Ensure account schemas/services reject credentials and API sync for `GENERIC` accounts and block populated type transitions to/from generic; serialize transitions against ingestion; tests for account creation and history guards

## 2. v1 schema and parser

- [x] 2.1 Create `backend/app/schemas/generic_import.py`: v1 DTOs with case-insensitive enums, UTC/overflow validation, conditional derivative identity and order marker/P&L requirements, OCC derivation, bounded split ratios, accurate pre-normalization input schema and `to_raw()` mapping per spec
- [x] 2.2 Add `GenericRowError` and `GenericImportValidationError(ImportParseError)` in `backend/app/core/exceptions.py`; error messages contain field names only, never values
- [x] 2.3 Implement generic CSV/JSON detection/adapters and collect-all validation, including linear header counts, required/version/duplicate checks, multi-leg identity and consistent order markers; keep unknown columns inert and warn once
- [x] 2.4 Preserve the original record under inert `raw_data.source_record`; populate trusted top-level `brokerageOrderID` and derived `netCash` only from validated `order_id`; shared constants cross-reference `get_combo_order_meta_by_raw_ids`
- [x] 2.5 Register `BrokerType.GENERIC` and `GenericParser` in `backend/app/utils/parsers/factory.py`
- [x] 2.6 Unit tests for every format/validation scenario plus derivative/order requirements, reserved-field isolation, ratio precision, UTC overflow, wide headers and input-schema consistency

## 3. Import pipeline integration

- [x] 3.1 Scope generic raw IDs by account before user stamping, require the generic target account, and scope metadata protection to that account so another account cannot suppress its projection
- [x] 3.2 In `backend/app/api/v1/imports.py`: map `AccountType.GENERIC → BrokerType.GENERIC`, allow `{"csv", "json"}`, update route description/docstring
- [x] 3.3 Catch `GenericImportValidationError` in the upload route and return HTTP 400 with `detail = {"message", "errors"}` (capped list); keep other error responses unchanged
- [x] 3.4 API/integration tests: generic uploads/reimports/overlaps, protected cross-account isolation, populated namespace transitions, derivative and combo P&L correctness, inert reserved extras, format restrictions, structured errors and no persistence on failure
- [x] 3.5 Verify audit logging: upload covered by middleware; confirm no row values leak into logs or error details

## 4. Schema and template downloads

- [x] 4.1 New router `backend/app/api/v1/generic_import.py` at `/api/v1/imports/generic` with `GET /schema` (`application/schema+json`) and `GET /template.csv` (`text/csv` attachment, equity round trip + option spread with `order_id`); register in the v1 router
- [x] 4.2 Tests: schema lists v1 fields/conditional requirements and accepts advertised input forms; template header equals field list and imports cleanly into an empty generic account
- [x] 4.3 OpenAPI annotations for new/changed endpoints, including stricter derivative/order and account-transition requirements (`openapi-docs` agent)

## 5. Parity fixtures and tests

- [x] 5.1 Create `tools/convert_to_generic.py` (native fixture → generic CSV + JSON via native `parse_raw()`, deterministic output, CLI usage documented in module docstring)
- [x] 5.2 Generate `tests/test_data/generic/` from `tests/test_data/ibkr/TraderJournal_anonymized_combined.xml`, `tests/test_data/ibkr/combo_positions.xml`, `tests/test_data/schwab/schwab_aggregation.json`, `tests/test_data/schwab/schwab_short_roundtrip.json` (separate fixture-only commit waived during implementation; final feature commit subsequently authorized by user)
- [x] 5.3 Test: re-running the converter reproduces the committed fixtures byte-for-byte
- [x] 5.4 Integration tests `backend/tests/integration/test_generic_import_parity.py`: native vs generic (IBKR combined, IBKR combos, Schwab aggregation, Schwab short roundtrip) and CSV vs JSON equivalence; compare fields listed in the spec, `net_cash` rounded to 2 decimals
- [x] 5.5 Analyse any parity mismatch; fix the mapping or document it as a known limitation in `design.md` and the README before merge

## 6. Frontend

- [x] 6.1 Regenerate the OpenAPI client (`openapi-sync` skill); add `"generic"` to `AccountType` usages and synchronize final input/transition documentation
- [x] 6.2 Add "Other broker (CSV/JSON)" option to `BrokerAccountCreateModal`, `AccountForm`; handle `generic` in `BrokerAccountEditModal` (no credentials, no sync), `AccountList` badge, `TradeFilterBar` if account types are listed
- [x] 6.3 `ImportForm`: accept `.csv,.json` for generic accounts, format hint with links to the help entry and both downloads, render structured row errors from `detail.errors`
- [x] 6.4 Frontend tests for the import form (accept attribute, hint, error list rendering) and account creation with type `generic`

## 7. Documentation

- [x] 7.1 Complete the format reference with final derivative/order requirements, inert source metadata, numeric/date limits, account transition/reset guidance, examples, flips, converter and parity limitation
- [x] 7.2 Synchronize in-app other-broker help with final rules and authenticated downloads; regenerate `docs/HELP_GLOSSARY.md`
- [x] 7.3 Synchronize API usage, AGENTS broker-ingestion guidance and README discoverability with final generic import/account contracts
- [x] 7.4 Append to `CHANGELOG.md` `[Unreleased]` (`changelog-writer` agent)

## 8. Verification

- [x] 8.1 `ruff check`, `ruff format`, `mypy --strict` clean after review corrections (`lint-check` skill)
- [x] 8.2 Full backend test suite green after review regressions (`unit-test` skill); frontend lint, type-check and Jest green
- [x] 8.3 Code review (`code-reviewer` agent) and UI review of the import form and account modal (`ui-reviewer`)
