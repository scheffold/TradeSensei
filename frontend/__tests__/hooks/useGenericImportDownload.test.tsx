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
import { useGenericImportDownload } from "@/hooks/useGenericImportDownload";
import {
  getGenericImportSchemaApiV1ImportsGenericSchemaGet,
  getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet,
} from "@/lib/api";

// Explicit factories also work before the new endpoints are generated in task 6.1.
jest.mock("@/lib/api", () => ({
  getGenericImportSchemaApiV1ImportsGenericSchemaGet: jest.fn(),
  getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet: jest.fn(),
}));

const mockSchema = getGenericImportSchemaApiV1ImportsGenericSchemaGet as jest.Mock;
const mockTemplate = getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet as jest.Mock;
const mockCreateObjectURL = jest.fn();
const mockRevokeObjectURL = jest.fn();
const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false, gcTime: Infinity },
    },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useGenericImportDownload", () => {
  beforeAll(() => {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: mockCreateObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: mockRevokeObjectURL });
  });

  beforeEach(() => {
    jest.useFakeTimers();
    jest.resetAllMocks();
    mockCreateObjectURL.mockReturnValue("blob:generic-import-download");
    jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    act(() => { jest.runOnlyPendingTimers(); });
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  afterAll(() => {
    if (originalCreateObjectURL) Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
    else Reflect.deleteProperty(URL, "createObjectURL");
    if (originalRevokeObjectURL) Object.defineProperty(URL, "revokeObjectURL", originalRevokeObjectURL);
    else Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it.each([
    { kind: "schema" as const, filename: "generic-import-schema.json", type: "application/schema+json" },
    { kind: "template" as const, filename: "generic-import-template.csv", type: "text/csv" },
  ])("downloads $kind through the SDK blob path and releases its temporary URL", async ({ kind, filename, type }) => {
    const blob = new Blob(["download body"], { type });
    const selectedEndpoint = kind === "schema" ? mockSchema : mockTemplate;
    const otherEndpoint = kind === "schema" ? mockTemplate : mockSchema;
    selectedEndpoint.mockResolvedValue({ data: blob, error: null });
    const downloadLinks: HTMLAnchorElement[] = [];
    jest.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadLinks.push(this);
      expect(this.isConnected).toBe(true);
      expect(this.href).toBe("blob:generic-import-download");
      expect(this.download).toBe(filename);
    });
    const { result } = renderHook(() => useGenericImportDownload(), { wrapper: makeWrapper() });

    await act(async () => { await result.current.mutateAsync(kind); });

    // The shared SDK supplies authentication; the browser navigates only to the returned blob.
    expect(selectedEndpoint).toHaveBeenCalledTimes(1);
    expect(selectedEndpoint).toHaveBeenCalledWith({ parseAs: "blob" });
    expect(otherEndpoint).not.toHaveBeenCalled();
    expect(mockCreateObjectURL).toHaveBeenCalledTimes(1);
    expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1);
    expect(downloadLinks).toHaveLength(1);
    expect(downloadLinks[0].isConnected).toBe(false);
    expect(document.querySelector("a[download]")).not.toBeInTheDocument();
    expect(mockRevokeObjectURL).not.toHaveBeenCalled();

    act(() => { jest.runOnlyPendingTimers(); });
    expect(mockRevokeObjectURL).toHaveBeenCalledTimes(1);
    expect(mockRevokeObjectURL).toHaveBeenCalledWith("blob:generic-import-download");
  });

  it("exposes the requested kind and pending state until the SDK returns", async () => {
    let finishDownload!: (value: { data: Blob; error: null }) => void;
    mockTemplate.mockReturnValue(new Promise((resolve) => { finishDownload = resolve; }));
    const { result } = renderHook(() => useGenericImportDownload(), { wrapper: makeWrapper() });

    act(() => { result.current.mutate("template"); });

    await waitFor(() => expect(result.current.isPending).toBe(true));
    expect(result.current.variables).toBe("template");
    expect(mockCreateObjectURL).not.toHaveBeenCalled();

    await act(async () => { finishDownload({ data: new Blob(["csv"]), error: null }); });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("preserves authentication errors without creating an unauthenticated download link", async () => {
    const apiError = { detail: "Not authenticated" };
    mockSchema.mockResolvedValue({ data: null, error: apiError });
    const { result } = renderHook(() => useGenericImportDownload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync("schema")).rejects.toBe(apiError);
    });

    await waitFor(() => expect(result.current.error).toBe(apiError));
    expect(mockCreateObjectURL).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
    expect(mockRevokeObjectURL).not.toHaveBeenCalled();
  });

  it.each([undefined, null, {}, "not a blob"])("rejects a successful response with non-blob data %p", async (data) => {
    mockTemplate.mockResolvedValue({ data, error: null });
    const { result } = renderHook(() => useGenericImportDownload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync("template")).rejects.toThrow("Download failed.");
    });

    expect(mockCreateObjectURL).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
    expect(mockRevokeObjectURL).not.toHaveBeenCalled();
  });

  it("allows retry after a transport failure and cleans up the successful retry", async () => {
    const networkError = new Error("Network unavailable");
    mockSchema
      .mockRejectedValueOnce(networkError)
      .mockResolvedValueOnce({ data: new Blob(["schema"]), error: null });
    const { result } = renderHook(() => useGenericImportDownload(), { wrapper: makeWrapper() });

    await act(async () => {
      await expect(result.current.mutateAsync("schema")).rejects.toBe(networkError);
    });
    expect(mockCreateObjectURL).not.toHaveBeenCalled();

    await act(async () => { await result.current.mutateAsync("schema"); });
    act(() => { jest.runOnlyPendingTimers(); });

    expect(mockSchema).toHaveBeenCalledTimes(2);
    expect(mockCreateObjectURL).toHaveBeenCalledTimes(1);
    expect(mockRevokeObjectURL).toHaveBeenCalledTimes(1);
    expect(mockRevokeObjectURL).toHaveBeenCalledWith("blob:generic-import-download");
  });
});
