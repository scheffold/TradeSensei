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
import { fireEvent, render, screen, within } from "@testing-library/react";
import { HelpClient } from "@/app/help/HelpClient";
import { useGenericImportDownload } from "@/hooks/useGenericImportDownload";
import { findHelpEntry, HELP_SECTIONS } from "@/lib/help-content";
import type { HelpSection } from "@/lib/help-content";

jest.mock("@/hooks/useGenericImportDownload", () => ({
  useGenericImportDownload: jest.fn(),
}));

// Rendering markdown is covered separately; avoid its ESM dependencies here.
jest.mock("@/components/notes/MarkdownRenderer", () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div>{content}</div>,
}));

const mockUseGenericImportDownload = useGenericImportDownload as jest.Mock;
const mockDownload = jest.fn();
const genericSections = HELP_SECTIONS.filter((section) =>
  section.entries.some((entry) => entry.id === "generic-import-format"),
);

function renderHelp(sections: HelpSection[] = genericSections) {
  return render(<HelpClient sections={sections} version="test" />);
}

describe("HelpClient generic import downloads", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    mockUseGenericImportDownload.mockReturnValue({
      mutate: mockDownload,
      isPending: false,
      variables: undefined,
      error: null,
    });
  });

  it("places both download actions only in the dictionary entry targeted by the Import-page help hint", () => {
    renderHelp(HELP_SECTIONS);

    const formatEntry = findHelpEntry("generic-import-format");
    expect(formatEntry).toMatchObject({ label: "Generic import format" });
    expect(HELP_SECTIONS.flatMap((section) => section.entries)
      .filter((entry) => entry.id === "generic-import-format")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 2, name: "Importing from other brokers" })).toBeInTheDocument();

    const formatCard = screen.getByRole("heading", { level: 3, name: "Generic import format" }).closest("section");
    // ImportForm.upload.test.tsx checks that the hint links to /help#generic-import-format.
    expect(formatCard).toHaveAttribute("id", "generic-import-format");
    const downloads = screen.getByRole("group", { name: "Generic import downloads" });
    expect(downloads.closest("section")).toBe(formatCard);
    expect(screen.getAllByRole("group", { name: "Generic import downloads" })).toHaveLength(1);
    expect(within(downloads).getAllByRole("button")).toHaveLength(2);
    for (const name of ["Download JSON Schema", "Download CSV template"]) {
      expect(screen.getAllByRole("button", { name })).toHaveLength(1);
      const action = within(downloads).getByRole("button", { name });
      expect(action).toBeEnabled();
      expect(action).toHaveAttribute("type", "button");
      expect(action).toHaveClass("underline", "focus-visible:ring-2");
      expect(action).not.toHaveAttribute("href");
    }
    expect(within(downloads).queryByRole("link")).not.toBeInTheDocument();

    for (const id of ["generic-execution-id", "generic-open-close", "generic-multi-leg-orders"]) {
      const entry = findHelpEntry(id);
      expect(entry).toBeDefined();
      const card = screen.getByRole("heading", { level: 3, name: entry!.label }).closest("section");
      expect(card).toHaveAttribute("id", id);
      expect(card).not.toContainElement(downloads);
    }
    expect(mockUseGenericImportDownload).toHaveBeenCalled();
    expect(mockDownload).not.toHaveBeenCalled();
  });

  it.each([
    { name: "Download JSON Schema", kind: "schema" },
    { name: "Download CSV template", kind: "template" },
  ])("delegates $kind to the authenticated download hook rather than navigating to a bare API URL", ({ name, kind }) => {
    renderHelp();

    fireEvent.click(screen.getByRole("button", { name }));

    expect(mockDownload).toHaveBeenCalledTimes(1);
    expect(mockDownload).toHaveBeenCalledWith(kind);
  });

  it.each([
    { kind: "schema", message: "Downloading JSON Schema…" },
    { kind: "template", message: "Downloading CSV template…" },
  ])("disables both actions and announces the pending $kind download", ({ kind, message }) => {
    mockUseGenericImportDownload.mockReturnValue({
      mutate: mockDownload,
      isPending: true,
      variables: kind,
      error: null,
    });
    renderHelp();
    const downloads = screen.getByRole("group", { name: "Generic import downloads" });

    expect(within(downloads).getByRole("status")).toHaveTextContent(message);
    for (const button of within(downloads).getAllByRole("button")) {
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(within(downloads).queryByRole("alert")).not.toBeInTheDocument();
    expect(mockDownload).not.toHaveBeenCalled();
  });

  it.each([
    { error: { detail: "Not authenticated" }, message: "Not authenticated" },
    {
      error: { detail: { message: "Session expired. Please sign in again.", debug: { token: "private-token" } } },
      message: "Session expired. Please sign in again.",
    },
    {
      error: { detail: { errors: [{ input: "private-execution-value" }] }, token: "private-token" },
      message: "Download failed. Please try again.",
    },
  ])("announces only a safe error message: $message", ({ error, message }) => {
    mockUseGenericImportDownload.mockReturnValue({
      mutate: mockDownload,
      isPending: false,
      variables: "schema",
      error,
    });
    renderHelp();
    const downloads = screen.getByRole("group", { name: "Generic import downloads" });
    const alert = within(downloads).getByRole("alert");

    expect(alert.textContent).toBe(message);
    expect(downloads).not.toHaveTextContent(/private-token|private-execution-value|\[object Object\]/);
    expect(within(downloads).queryByRole("status")).not.toBeInTheDocument();
    for (const button of within(downloads).getAllByRole("button")) expect(button).toBeEnabled();
    expect(mockDownload).not.toHaveBeenCalled();
  });

  it.each([
    "generic-execution-id",
    "generic-open-close",
    "generic-multi-leg-orders",
    "open-closed-filter",
  ])("does not mount downloads, pending status, or errors for unrelated entry %s", (id) => {
    const entry = findHelpEntry(id);
    expect(entry).toBeDefined();
    renderHelp([{ heading: "Related help", entries: [entry!] }]);

    expect(screen.getByRole("heading", { level: 3, name: entry!.label })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Generic import downloads" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download JSON Schema|Download CSV template/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mockUseGenericImportDownload).not.toHaveBeenCalled();
    expect(mockDownload).not.toHaveBeenCalled();
  });
});
