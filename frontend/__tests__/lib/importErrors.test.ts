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

import { extractImportRowErrors } from "@/lib/importErrors";

describe("extractImportRowErrors", () => {
  it("preserves CSV line numbers, JSON locations, and error order without exposing other row data", () => {
    const csvError = { location: 12, field: "executed_at", message: "A timezone offset is required" };
    const jsonError = { location: "executions[56]", field: "instrument_id", message: "A contract identifier is required" };
    const error = {
      detail: {
        message: "Generic import validation failed",
        errors: [
          { ...csvError, input: "private timestamp", context: { quantity: "98765.4321" } },
          { ...jsonError, price: "12345.6789", realized_pnl: "-987.65" },
        ],
      },
    };

    expect(extractImportRowErrors(error)).toEqual([csvError, jsonError]);
    expect(error.detail.errors[0]).toHaveProperty("input", "private timestamp");
  });

  it.each([
    null,
    undefined,
    "Native broker import failed",
    400,
    new Error("Network unavailable"),
    {},
    { detail: null },
    { detail: "Invalid XML file" },
    { detail: [{ loc: ["body", "file"], msg: "Field required" }] },
    { detail: { message: "Invalid file" } },
    { detail: { errors: "Not an error list" } },
    { detail: { errors: {} } },
    { detail: { errors: [] } },
  ])("returns no row errors for unsupported or legacy error shape %p", (error) => {
    expect(extractImportRowErrors(error)).toEqual([]);
  });

  it("skips malformed entries while retaining valid safe messages", () => {
    const valid = { location: 2, field: "execution_id", message: "Field required" };
    const malformed = [
      null,
      "arbitrary text",
      {},
      { field: "price", message: "Invalid" },
      { location: {}, field: "price", message: "Invalid" },
      { location: 3, field: ["price"], message: "Invalid" },
      { location: 4, field: "price", message: { input: "private value" } },
      { location: 5, message: "Invalid" },
      { location: 6, field: "price" },
    ];

    expect(extractImportRowErrors({ detail: { errors: [malformed[0], valid, ...malformed.slice(1)] } })).toEqual([valid]);
  });
});
