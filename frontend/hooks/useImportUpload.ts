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
import { uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost } from "@/lib/api";
import type { ImportResult } from "@/lib/types";

interface ImportUploadPayload {
  portfolioId: string | null;
  accountId: string;
  file: File;
  overwriteManuallyEdited: boolean;
}

export function useImportUpload() {
  return useMutation<ImportResult, unknown, ImportUploadPayload>({
    mutationFn: async ({ portfolioId, accountId, file, overwriteManuallyEdited }) => {
      if (!portfolioId) throw new Error("No active portfolio");

      const { data, error } = await uploadImportApiV1PortfoliosPortfolioIdImportsUploadPost({
        path: { portfolio_id: portfolioId },
        body: {
          account_id: accountId,
          file,
          overwrite_manually_edited: overwriteManuallyEdited,
        },
      });
      if (error || !data) throw error ?? new Error("Upload failed.");
      return { ...data.data, warnings: data.data.warnings ?? [] };
    },
  });
}
