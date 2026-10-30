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

export interface ImportRowError {
  location: string | number;
  field: string;
  message: string;
}

/** Read only displayable row-error fields; never stringify arbitrary API error data. */
export function extractImportRowErrors(error: unknown): ImportRowError[] {
  if (!error || typeof error !== "object" || !("detail" in error)) return [];
  const { detail } = error;
  if (!detail || typeof detail !== "object" || !("errors" in detail) || !Array.isArray(detail.errors)) return [];

  return detail.errors.flatMap((row: unknown): ImportRowError[] => {
    if (!row || typeof row !== "object" || !("location" in row) || !("field" in row) || !("message" in row)) return [];
    const { location, field, message } = row;
    if ((typeof location !== "string" && typeof location !== "number") || typeof field !== "string" || typeof message !== "string") return [];
    return [{ location, field, message }];
  });
}
