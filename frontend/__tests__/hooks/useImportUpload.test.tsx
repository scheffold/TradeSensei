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
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useImportUpload } from "@/hooks/useImportUpload";
import { uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost } from "@/lib/api";
import type { ImportResult } from "@/lib/types";

jest.mock("@/lib/api", () => ({
  uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost: jest.fn(),
}));

const mockUpload = uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost as jest.Mock;
const importResult: ImportResult = {
  broker: "generic",
  filename: "executions.csv",
  trades_parsed: 5,
  trades_created: 2,
  trades_skipped: 1,
  trades_protected_reconciled: 2,
  warnings: ["Ignored unknown column broker_note"],
  activity_log: ["created: trade-1"],
};

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function makePayload(overwriteManuallyEdited = false) {
  return {
    portfolioId: "portfolio-1",
    accountId: "generic-account",
    file: new File(["execution_id,symbol"], "executions.csv", { type: "text/csv" }),
    overwriteManuallyEdited,
  };
}

describe("useImportUpload", () => {
  beforeEach(() => {
    mockUpload.mockReset();
    mockUpload.mockResolvedValue({ data: { data: importResult }, error: null });
  });

  it.each([
    { filename: "executions.csv", type: "text/csv", accountId: "generic-account", overwrite: false },
    { filename: "executions.json", type: "application/json", accountId: "generic-account", overwrite: true },
    { filename: "flex.xml", type: "application/xml", accountId: "ibkr-account", overwrite: false },
    { filename: "transactions.json", type: "application/json", accountId: "schwab-account", overwrite: true },
  ])("passes $filename and its account to the SDK multipart serializer", async ({ filename, type, accountId, overwrite }) => {
    const file = new File(["broker export"], filename, { type });
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });
    let uploaded: ImportResult | undefined;

    await act(async () => {
      uploaded = await result.current.mutateAsync({
        portfolioId: "portfolio-1",
        accountId,
        file,
        overwriteManuallyEdited: overwrite,
      });
    });

    // Do not set Content-Type or stringify the File: the SDK owns the multipart boundary.
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(mockUpload).toHaveBeenCalledWith({
      path: { portfolio_id: "portfolio-1" },
      body: { account_id: accountId, file, overwrite_manually_edited: overwrite },
    });
    expect(mockUpload.mock.calls[0][0].body.file).toBe(file);
    expect(uploaded).toEqual(importResult);
  });

  it.each([undefined, null])("normalizes missing warnings (%p) while preserving result and protection metadata", async (warnings) => {
    mockUpload.mockResolvedValue({ data: { data: { ...importResult, warnings } }, error: null });
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync(makePayload())).resolves.toEqual({ ...importResult, warnings: [] });
    });
  });

  it.each([null, ""])("does not call the SDK without an active portfolio (%p)", async (portfolioId) => {
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync({ ...makePayload(), portfolioId })).rejects.toThrow("No active portfolio");
    });

    expect(mockUpload).not.toHaveBeenCalled();
  });

  it.each([
    { detail: "Invalid IBKR XML file" },
    { detail: { message: "Generic import validation failed", errors: [{ location: 2, field: "execution_id", message: "Field required" }] } },
  ])("preserves API errors for the form to render without losing row details", async (apiError) => {
    mockUpload.mockResolvedValue({ data: null, error: apiError });
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync(makePayload())).rejects.toBe(apiError);
    });

    await waitFor(() => expect(result.current.error).toBe(apiError));
    expect(result.current.isError).toBe(true);
  });

  it("reports an empty SDK response as a failed upload", async () => {
    mockUpload.mockResolvedValue({ data: undefined, error: undefined });
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync(makePayload())).rejects.toThrow("Upload failed.");
    });
  });

  it("propagates transport failures without retrying the import automatically", async () => {
    const error = new Error("Network unavailable");
    mockUpload.mockRejectedValue(error);
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync(makePayload())).rejects.toBe(error);
    });

    expect(mockUpload).toHaveBeenCalledTimes(1);
  });

  it("remains pending until the upload completes and then exposes the parsed result", async () => {
    let finishUpload!: (value: { data: { data: ImportResult }; error: null }) => void;
    mockUpload.mockReturnValue(new Promise((resolve) => { finishUpload = resolve; }));
    const { result } = renderHook(() => useImportUpload(), { wrapper: makeWrapper() });

    act(() => { result.current.mutate(makePayload()); });
    await waitFor(() => expect(result.current.isPending).toBe(true));
    expect(result.current.data).toBeUndefined();

    await act(async () => { finishUpload({ data: { data: importResult }, error: null }); });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(importResult);
  });
});
