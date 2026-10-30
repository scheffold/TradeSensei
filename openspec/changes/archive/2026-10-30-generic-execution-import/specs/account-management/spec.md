## MODIFIED Requirements

### Requirement: Account Type Specialization
The system MUST require an explicit `type` upon Account creation, strictly limited to an accepted enumeration (`MANUAL`, `SCHWAB`, `IBKR`, `GENERIC`). Account creation additionally MUST require a valid `portfolio_id` owned by the requesting user. The created account SHALL be linked to that portfolio, and its currency MUST be inherited from the portfolio's `base_currency`. The user SHALL NOT be prompted for currency selection.

A `GENERIC` account represents a broker without native support ("Other broker"). It SHALL NOT store broker credentials and SHALL NOT support API sync. It SHALL accept file uploads in the generic execution format (CSV or JSON) only.

Changing an account type to or from `GENERIC` SHALL be rejected while any raw
transactions, logical executions or trades remain, including suppressed/deleted
history. This also applies to transitions through `MANUAL`, to prevent mixing
native and generic execution namespaces. A cleared/empty account MAY change type
subject to credential restrictions. The history check and type update SHALL be
serialized against imports for the same account.

#### Scenario: Creating a new account with a valid type
- **WHEN** the user creates an account with type `SCHWAB` and a valid `portfolio_id`
- **THEN** the system SHALL save the account and assign the `SCHWAB` type
- **THEN** the account's `portfolio_id` SHALL be set to the provided value
- **THEN** the account's `currency` SHALL automatically be set to the portfolio's `base_currency`

#### Scenario: Creating a generic account
- **WHEN** the user creates an account with type `GENERIC` and a valid `portfolio_id`
- **THEN** the system SHALL save the account with type `GENERIC` and no credentials
- **THEN** the account SHALL NOT offer API sync

#### Scenario: Generic account accepts only generic files
- **WHEN** the user uploads a `.csv` or `.json` file to a `GENERIC` account
- **THEN** the system SHALL import it with the generic parser
- **WHEN** the user uploads a `.xml` file to a `GENERIC` account
- **THEN** the system SHALL reject it with HTTP 422

#### Scenario: Populated accounts cannot cross the generic boundary
- **WHEN** an account with raw transactions, logical executions or trades is changed to or from `GENERIC`, even without credentials
- **THEN** the update SHALL be rejected without modifying its type or history
- **THEN** re-importing an equivalent export through a second namespace SHALL NOT double-count the account's holdings

#### Scenario: Creating an account without portfolio_id rejected
- **WHEN** the user creates an account without providing `portfolio_id`
- **THEN** the system SHALL return HTTP 422 Unprocessable Entity

#### Scenario: Creating an account with foreign portfolio_id rejected
- **WHEN** the user creates an account with a `portfolio_id` that belongs to another user
- **THEN** the system SHALL return HTTP 404
