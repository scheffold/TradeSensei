/*
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AccountForm } from "@/components/accounts/AccountForm";
import { client } from "@/lib/api/client.gen";
import { getAccountCredentialsApiV1PortfoliosPortfolioIdAccountsAccountIdCredentialsGet } from "@/lib/api/index";
import type { Account } from "@/lib/types";

jest.mock("@/hooks/useActivePortfolio", () => ({
  useActivePortfolio: () => ({ activePortfolioId: "portfolio-1" }),
}));
jest.mock("@/stores/portfolioStore", () => ({
  usePortfolioStore: (selector: (state: { activePortfolioId: string }) => unknown) =>
    selector({ activePortfolioId: "portfolio-1" }),
}));
jest.mock("@/lib/api/client.gen", () => ({
  client: { post: jest.fn(), patch: jest.fn() },
}));
jest.mock("@/lib/api/index", () => ({
  getAccountCredentialsApiV1PortfoliosPortfolioIdAccountsAccountIdCredentialsGet: jest.fn(),
}));

const genericAccount: Account = {
  id: "generic-account",
  user_id: "user-1",
  portfolio_id: "portfolio-1",
  name: "Other Broker",
  account_number: null,
  type: "generic",
  currency: "EUR",
  is_active: true,
  has_api_keys: false,
  settings: { sync_days: 30, flex_from_date_preset: "ytd", flex_custom_from_date: null },
  created_at: "2026-01-01T00:00:00Z",
};

describe("AccountForm generic accounts", () => {
  const onSave = jest.fn();
  const onCancel = jest.fn();

  beforeEach(() => {
    jest.resetAllMocks();
    (client.post as jest.Mock).mockResolvedValue({ data: { data: genericAccount }, error: null });
    (client.patch as jest.Mock).mockResolvedValue({ data: { data: genericAccount }, error: null });
  });

  function renderForm(account?: Account) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <AccountForm account={account} onSave={onSave} onCancel={onCancel} />
      </QueryClientProvider>,
    );
  }

  function selectGeneric() {
    fireEvent.change(screen.getByRole("combobox", { name: /Account Type/ }), {
      target: { value: "generic" },
    });
  }

  it("offers the generic type and file-import guidance without credential or sync controls", () => {
    renderForm();
    expect(screen.getByRole("option", { name: "Other broker (CSV/JSON)" })).toHaveValue("generic");
    selectGeneric();

    expect(screen.getByText(/This account does not use credentials or API sync/)).toBeInTheDocument();
    expect(screen.queryByText("API Credentials")).not.toBeInTheDocument();
    expect(screen.queryByText("Default Sync Settings")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Default look-back")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Flex date range preset")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/currency/i)).not.toBeInTheDocument();
  });

  it("uses native required-name validation before creating a generic account", async () => {
    renderForm();
    selectGeneric();
    await userEvent.click(screen.getByRole("button", { name: "Create Account" }));

    expect(screen.getByRole("textbox", { name: /Name \*/ })).toBeInvalid();
    expect(client.post).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("creates the generic account in the active portfolio without stale native credentials or settings", async () => {
    renderForm();
    fireEvent.change(screen.getByRole("combobox", { name: /Account Type/ }), {
      target: { value: "ibkr" },
    });
    fireEvent.change(screen.getByLabelText("Flex Token"), { target: { value: "do-not-submit-token" } });
    fireEvent.change(screen.getByLabelText("Flex Query ID"), { target: { value: "do-not-submit-query" } });
    fireEvent.change(screen.getByLabelText("Default look-back"), { target: { value: "90" } });
    selectGeneric();
    fireEvent.change(screen.getByRole("textbox", { name: /Name \*/ }), {
      target: { value: "  Other Broker  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(client.post).toHaveBeenCalledWith({
      url: "/api/v1/portfolios/portfolio-1/accounts",
      body: {
        name: "Other Broker",
        account_number: null,
        type: "generic",
        portfolio_id: "portfolio-1",
        properties: null,
        settings: undefined,
      },
    });
    expect(getAccountCredentialsApiV1PortfoliosPortfolioIdAccountsAccountIdCredentialsGet).not.toHaveBeenCalled();
  });

  it("shows a saving state and prevents duplicate generic-account submissions", async () => {
    let finishCreate!: (value: { data: { data: Account }; error: null }) => void;
    (client.post as jest.Mock).mockReturnValueOnce(new Promise((resolve) => { finishCreate = resolve; }));
    renderForm();
    selectGeneric();
    fireEvent.change(screen.getByRole("textbox", { name: /Name \*/ }), {
      target: { value: "Other Broker" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    expect(await screen.findByRole("button", { name: /Saving…/ })).toBeDisabled();
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Saving…/ }));
    expect(client.post).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();

    await act(async () => { finishCreate({ data: { data: genericAccount }, error: null }); });
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  });

  it("renders API validation errors without reporting a successful creation", async () => {
    (client.post as jest.Mock).mockResolvedValueOnce({
      data: null,
      error: { detail: { message: "Account name already exists" } },
    });
    renderForm();
    selectGeneric();
    fireEvent.change(screen.getByRole("textbox", { name: /Name \*/ }), {
      target: { value: "Other Broker" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Account" }));

    expect(await screen.findByText("Account name already exists")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create Account" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: /Name \*/ })).toHaveValue("Other Broker");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("edits generic accounts without loading credentials or sending sync settings", async () => {
    renderForm({
      ...genericAccount,
      has_api_keys: true,
      settings: { sync_days: 90, flex_from_date_preset: "ytd", flex_custom_from_date: null },
    });

    expect(screen.getByRole("combobox", { name: /Account Type/ })).toHaveValue("generic");
    expect(screen.queryByText("API Credentials")).not.toBeInTheDocument();
    expect(screen.queryByText("Default Sync Settings")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: /Name \*/ }), {
      target: { value: "Renamed Broker" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(client.patch).toHaveBeenCalledWith({
      url: "/api/v1/portfolios/portfolio-1/accounts/generic-account",
      body: {
        name: "Renamed Broker",
        account_number: null,
        type: "generic",
        portfolio_id: "portfolio-1",
        properties: null,
        settings: undefined,
      },
    });
    expect(getAccountCredentialsApiV1PortfoliosPortfolioIdAccountsAccountIdCredentialsGet).not.toHaveBeenCalled();
    expect(client.post).not.toHaveBeenCalled();
  });

  it("cancels without submitting an account", () => {
    renderForm();
    selectGeneric();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(client.post).not.toHaveBeenCalled();
  });
});
