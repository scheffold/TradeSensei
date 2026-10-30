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
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ImportForm } from "@/components/imports/ImportForm";
import { useAccounts, useUpdateAccount } from "@/hooks/useAccounts";
import {
  getGenericImportSchemaApiV1ImportsGenericSchemaGet,
  getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet,
  syncAccountApiV1BrokerSyncAccountIdPost,
  uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost,
} from "@/lib/api";
import type { Account, ImportResult } from "@/lib/types";

jest.mock("@/hooks/useAccounts", () => ({
  useAccounts: jest.fn(),
  useUpdateAccount: jest.fn(),
}));
jest.mock("@/stores/portfolioStore", () => ({
  usePortfolioStore: (selector: (state: { activePortfolioId: string }) => unknown) =>
    selector({ activePortfolioId: "portfolio-1" }),
}));
jest.mock("@/lib/api/client.gen", () => ({ client: {} }));
jest.mock("@/lib/api", () => ({
  getGenericImportSchemaApiV1ImportsGenericSchemaGet: jest.fn(),
  getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet: jest.fn(),
  syncAccountApiV1BrokerSyncAccountIdPost: jest.fn(),
  uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost: jest.fn(),
}));

const mockUseAccounts = useAccounts as jest.Mock;
const mockUseUpdateAccount = useUpdateAccount as jest.Mock;
const mockUpload = uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost as jest.Mock;
const mockSchema = getGenericImportSchemaApiV1ImportsGenericSchemaGet as jest.Mock;
const mockTemplate = getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet as jest.Mock;
const mockSaveSettings = jest.fn();
const mockCreateObjectURL = jest.fn();
const mockRevokeObjectURL = jest.fn();
const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    id: "generic-account",
    user_id: "user-1",
    portfolio_id: "portfolio-1",
    name: "Other Broker",
    account_number: null,
    type: "generic",
    currency: "USD",
    is_active: true,
    has_api_keys: false,
    settings: { sync_days: 30, flex_from_date_preset: "ytd", flex_custom_from_date: null },
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function uploadResponse(overrides: Partial<ImportResult> = {}) {
  return {
    data: {
      data: {
        broker: "generic",
        filename: "executions.csv",
        trades_parsed: 3,
        trades_created: 2,
        trades_skipped: 1,
        warnings: [],
        ...overrides,
      } satisfies ImportResult,
    },
    error: null,
  };
}

function renderForm() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateQueries = jest.spyOn(queryClient, "invalidateQueries");
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <ImportForm />
    </QueryClientProvider>,
  );
  return { ...rendered, invalidateQueries };
}

function chooseFiles(...files: File[]) {
  fireEvent.change(screen.getByLabelText("Broker export files"), { target: { files } });
}

describe("ImportForm file uploads", () => {
  beforeAll(() => {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: mockCreateObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: mockRevokeObjectURL });
  });

  beforeEach(() => {
    jest.resetAllMocks();
    mockUseAccounts.mockReturnValue({ data: [makeAccount()], isLoading: false, isError: false });
    mockUseUpdateAccount.mockReturnValue({ mutateAsync: mockSaveSettings, isPending: false });
    mockUpload.mockResolvedValue(uploadResponse());
    mockCreateObjectURL.mockReturnValue("blob:generic-import-download");
    jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  afterAll(() => {
    if (originalCreateObjectURL) Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
    else Reflect.deleteProperty(URL, "createObjectURL");
    if (originalRevokeObjectURL) Object.defineProperty(URL, "revokeObjectURL", originalRevokeObjectURL);
    else Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it("shows account loading before offering file selection", () => {
    mockUseAccounts.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    renderForm();

    expect(screen.getByText("Loading accounts…")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Broker export files")).not.toBeInTheDocument();
  });

  it("accepts generic CSV/JSON and associates the execution-format guidance with the file input", () => {
    renderForm();

    expect(screen.getByRole("combobox")).toHaveValue("generic-account");
    expect(screen.getByRole("option", { name: "Other Broker (Other broker (CSV/JSON))" })).toBeInTheDocument();
    const fileInput = screen.getByLabelText("Broker export files");
    expect(fileInput).toHaveAttribute("accept", ".csv,.json");
    expect(fileInput).toHaveAttribute("multiple");
    expect(fileInput).toHaveAccessibleDescription(/one record per execution.*stable execution_id.*timezone/);
    expect(screen.getByText(/CSV or JSON files \(generic execution format\)/)).toBeInTheDocument();
    expect(screen.queryByText(/No API credentials stored/)).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Sync via API" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Sync .* Trades/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save as default for this account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Import \d/ })).not.toBeInTheDocument();
  });

  it("provides a help link and keyboard-accessible link-styled SDK download actions", () => {
    renderForm();

    const help = screen.getByRole("link", { name: "Generic import format help" });
    expect(help).toHaveAttribute("href", "/help#generic-import-format");
    expect(help).toHaveClass("underline", "focus-visible:ring-2");
    for (const name of ["Download JSON Schema", "Download CSV template"]) {
      const action = screen.getByRole("button", { name });
      expect(action).toBeEnabled();
      expect(action).toHaveAttribute("type", "button");
      expect(action).toHaveClass("underline", "focus-visible:ring-2");
      expect(action).not.toHaveAttribute("href");
    }
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mockSchema).not.toHaveBeenCalled();
    expect(mockTemplate).not.toHaveBeenCalled();
  });

  it.each([
    { type: "ibkr" as const, accept: ".xml", hint: "XML (IBKR Flex Query)" },
    { type: "schwab" as const, accept: ".json", hint: "JSON files (Schwab Transactions)" },
  ])("retains the native $type file restriction without generic help or downloads", ({ type, accept, hint }) => {
    mockUseAccounts.mockReturnValue({ data: [makeAccount({ type })], isLoading: false });
    renderForm();

    expect(screen.getByLabelText("Broker export files")).toHaveAttribute("accept", accept);
    expect(screen.getByText(`Import from broker export files (${hint}).`)).toBeInTheDocument();
    expect(screen.getByText(/No API credentials stored/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Generic import format help" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download JSON Schema|Download CSV template/ })).not.toBeInTheDocument();
  });

  it("does not offer file import for a manual account", () => {
    mockUseAccounts.mockReturnValue({ data: [makeAccount({ type: "manual" })], isLoading: false });
    renderForm();

    expect(screen.queryByLabelText("Broker export files")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Sync .* Trades/ })).not.toBeInTheDocument();
  });

  it("drops native sync controls when switching to a generic account even with stale API-key metadata", () => {
    mockUseAccounts.mockReturnValue({
      data: [
        makeAccount({ id: "ibkr-account", type: "ibkr", has_api_keys: true }),
        makeAccount({ has_api_keys: true }),
      ],
      isLoading: false,
    });
    renderForm();
    const accountSelect = screen.getByRole("combobox");
    fireEvent.change(accountSelect, { target: { value: "ibkr-account" } });
    expect(screen.getByRole("button", { name: "Sync IBKR Trades" })).toBeInTheDocument();

    fireEvent.change(accountSelect, { target: { value: "generic-account" } });

    expect(screen.getByLabelText("Broker export files")).toHaveAttribute("accept", ".csv,.json");
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByText("Import date range")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save as default for this account" })).not.toBeInTheDocument();
    expect(syncAccountApiV1BrokerSyncAccountIdPost).not.toHaveBeenCalled();
    expect(mockSaveSettings).not.toHaveBeenCalled();
  });

  it.each([
    { kind: "schema", name: "Download JSON Schema", pending: "Downloading JSON Schema…" },
    { kind: "template", name: "Download CSV template", pending: "Downloading CSV template…" },
  ])("shows pending and accessible error states for the $kind download", async ({ kind, name, pending }) => {
    let finishDownload!: (value: { data: null; error: { detail: string } }) => void;
    const endpoint = kind === "schema" ? mockSchema : mockTemplate;
    const otherEndpoint = kind === "schema" ? mockTemplate : mockSchema;
    endpoint.mockReturnValueOnce(new Promise((resolve) => { finishDownload = resolve; }));
    renderForm();

    fireEvent.click(screen.getByRole("button", { name }));
    expect(await screen.findByRole("button", { name: pending })).toBeDisabled();
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent(pending);
    expect(screen.getByRole("button", { name: kind === "schema" ? "Download CSV template" : "Download JSON Schema" })).toBeDisabled();
    expect(endpoint).toHaveBeenCalledWith({ parseAs: "blob" });
    expect(otherEndpoint).not.toHaveBeenCalled();

    await act(async () => { finishDownload({ data: null, error: { detail: "Your session has expired" } }); });

    expect(await screen.findByRole("alert")).toHaveTextContent("Your session has expired");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Your session has expired");
    expect(screen.getByRole("button", { name })).toBeEnabled();
    expect(screen.getByRole("button", { name: kind === "schema" ? "Download CSV template" : "Download JSON Schema" })).toBeEnabled();
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it.each([
    { kind: "schema", name: "Download JSON Schema", pending: "Downloading JSON Schema…" },
    { kind: "template", name: "Download CSV template", pending: "Downloading CSV template…" },
  ])("announces the pending $kind download politely and removes its status after completion", async ({ kind, name, pending }) => {
    let finishDownload!: (value: { data: Blob; error: null }) => void;
    const endpoint = kind === "schema" ? mockSchema : mockTemplate;
    endpoint.mockReturnValueOnce(new Promise((resolve) => { finishDownload = resolve; }));
    renderForm();

    fireEvent.click(screen.getByRole("button", { name }));
    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent(pending);
    expect(screen.getByRole("button", { name: pending })).toBeDisabled();
    expect(screen.getByRole("button", { name: kind === "schema" ? "Download CSV template" : "Download JSON Schema" })).toBeDisabled();

    await act(async () => { finishDownload({ data: new Blob(["download body"]), error: null }); });

    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Download JSON Schema" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Download CSV template" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() => expect(mockRevokeObjectURL).toHaveBeenCalledTimes(1));
  });

  it.each([
    { filename: "invalid.csv", type: "text/csv", firstLocation: 12, secondLocation: 57, firstLabel: "Line 12", secondLabel: "Line 57" },
    { filename: "invalid.json", type: "application/json", firstLocation: "executions[11]", secondLocation: "executions[56]", firstLabel: "executions[11]", secondLabel: "executions[56]" },
  ])("renders all safe structured errors from $filename in the FileQueue alert", async ({ filename, type, firstLocation, secondLocation, firstLabel, secondLabel }) => {
    mockUpload.mockResolvedValueOnce({
      data: null,
      error: {
        detail: {
          message: "Generic import validation failed",
          errors: [
            { location: firstLocation, field: "executed_at", message: "A timezone offset is required", input: "private row data" },
            { location: secondLocation, field: "instrument_id", message: "Distinct contracts require distinct identifiers", quantity: "98765.4321" },
          ],
        },
      },
    });
    renderForm();
    const file = new File(["invalid executions"], filename, { type });
    chooseFiles(file);
    fireEvent.click(screen.getByRole("button", { name: "Import 1 file" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Generic import validation failed");
    const errorList = within(alert).getByRole("list", { name: "Row validation errors" });
    const errors = within(errorList).getAllByRole("listitem");
    expect(errors).toHaveLength(2);
    expect(errors[0]).toHaveTextContent(`${firstLabel} — executed_at: A timezone offset is required`);
    expect(errors[1]).toHaveTextContent(`${secondLabel} — instrument_id: Distinct contracts require distinct identifiers`);
    expect(alert).not.toHaveTextContent(/private row data|98765\.4321|\[object Object\]/);
    expect(mockUpload).toHaveBeenCalledWith({
      path: { portfolio_id: "portfolio-1" },
      body: { account_id: "generic-account", file, overwrite_manually_edited: false },
    });
    expect(await screen.findByRole("button", { name: "Import more files" })).toBeInTheDocument();
  });

  it.each([
    { type: "ibkr" as const, filename: "flex.xml", message: "Invalid IBKR XML file" },
    { type: "schwab" as const, filename: "transactions.json", message: "Invalid Schwab JSON file" },
  ])("retains legacy string error details for $type uploads", async ({ type, filename, message }) => {
    mockUseAccounts.mockReturnValue({ data: [makeAccount({ type })], isLoading: false });
    mockUpload.mockResolvedValueOnce({ data: null, error: { detail: message } });
    renderForm();
    chooseFiles(new File(["invalid export"], filename));
    fireEvent.click(screen.getByRole("button", { name: "Import 1 file" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("list", { name: "Row validation errors" })).not.toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Import more files" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(filename)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Broker export files")).toBeInTheDocument();
  });

  it.each([
    { type: "ibkr" as const, filename: "flex.xml" },
    { type: "schwab" as const, filename: "transactions.json" },
  ])("still imports native $type files and displays result, protection, and warning details", async ({ type, filename }) => {
    mockUseAccounts.mockReturnValue({ data: [makeAccount({ id: "native-account", type })], isLoading: false });
    mockUpload.mockResolvedValueOnce(uploadResponse({
      broker: type,
      filename,
      trades_protected_reconciled: 1,
      warnings: ["Broker file warning"],
    }));
    renderForm();
    const file = new File(["native export"], filename);
    chooseFiles(file);
    fireEvent.click(screen.getByRole("button", { name: "Import 1 file" }));

    expect(await screen.findByText("Import complete — 1 file processed")).toBeInTheDocument();
    expect(screen.getByText("+2 created")).toBeInTheDocument();
    expect(screen.getByText("1 skipped")).toBeInTheDocument();
    expect(screen.getByText("1 protected")).toBeInTheDocument();
    expect(screen.getByText("1 warning")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Logs" }));
    expect(screen.getByText("Broker file warning")).toBeInTheDocument();
    expect(mockUpload).toHaveBeenCalledWith({
      path: { portfolio_id: "portfolio-1" },
      body: { account_id: "native-account", file, overwrite_manually_edited: false },
    });
  });

  it("processes files sequentially, continues after row errors, and invalidates imported-data caches", async () => {
    let finishFirst!: (value: { data: null; error: { detail: string } }) => void;
    mockUpload
      .mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve; }))
      .mockResolvedValueOnce(uploadResponse({ filename: "second.json" }));
    const { invalidateQueries } = renderForm();
    const first = new File(["bad CSV"], "first.csv", { type: "text/csv" });
    const second = new File(["valid JSON"], "second.json", { type: "application/json" });
    chooseFiles(first, second);
    expect(screen.getByText("2 files selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /Overwrite user modifications/ }));
    fireEvent.click(screen.getByRole("button", { name: "Import 2 files" }));

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Processing file 1 of 2…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel remaining files" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Broker export files")).not.toBeInTheDocument();

    await act(async () => { finishFirst({ data: null, error: { detail: "First file failed" } }); });

    expect(await screen.findByText("Import complete — 2 files processed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("First file failed");
    expect(screen.getByText("+2 created")).toBeInTheDocument();
    expect(mockUpload).toHaveBeenNthCalledWith(1, {
      path: { portfolio_id: "portfolio-1" },
      body: { account_id: "generic-account", file: first, overwrite_manually_edited: true },
    });
    expect(mockUpload).toHaveBeenNthCalledWith(2, {
      path: { portfolio_id: "portfolio-1" },
      body: { account_id: "generic-account", file: second, overwrite_manually_edited: true },
    });
    for (const key of ["trades", "metrics", "tax", "stats", "accounts"]) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: [key] });
    }
  });

  it("cancels the remaining native files without interrupting the in-flight SDK upload", async () => {
    mockUseAccounts.mockReturnValue({ data: [makeAccount({ type: "ibkr" })], isLoading: false });
    let finishUpload!: (value: ReturnType<typeof uploadResponse>) => void;
    mockUpload.mockReturnValueOnce(new Promise((resolve) => { finishUpload = resolve; }));
    renderForm();
    const first = new File(["native export"], "first.xml");
    chooseFiles(first, new File(["native export"], "second.xml"));
    fireEvent.click(screen.getByRole("button", { name: "Import 2 files" }));
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Cancel remaining files" }));
    await act(async () => { finishUpload(uploadResponse({ broker: "ibkr", filename: "first.xml" })); });

    expect(await screen.findByRole("button", { name: "Import more files" })).toBeInTheDocument();
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(screen.getByText("+2 created")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel remaining files" })).not.toBeInTheDocument();
  });
});
