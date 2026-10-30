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

"use client";

import { useMutation } from "@tanstack/react-query";
import {
  getGenericImportSchemaApiV1ImportsGenericSchemaGet,
  getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet,
} from "@/lib/api";

type GenericImportDownload = "schema" | "template";

export function useGenericImportDownload() {
  return useMutation<void, unknown, GenericImportDownload>({
    mutationFn: async (kind) => {
      const result = kind === "schema"
        ? await getGenericImportSchemaApiV1ImportsGenericSchemaGet({ parseAs: "blob" })
        : await getGenericImportTemplateApiV1ImportsGenericTemplateCsvGet({ parseAs: "blob" });
      // parseAs controls the runtime body format, regardless of the generated response type.
      const blob: unknown = result.data;
      if (result.error || !(blob instanceof Blob)) {
        throw result.error ?? new Error("Download failed.");
      }

      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = kind === "schema" ? "generic-import-schema.json" : "generic-import-template.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    },
  });
}
