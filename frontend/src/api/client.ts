import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Typed client. Paths, parameters and response bodies all come from the backend's
 *  OpenAPI schema (npm run gen:api), so a changed API fails `tsc`, not a demo. */
export const api = createClient<paths>({ baseUrl: "" });

export type Schemas = components["schemas"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Turn an openapi-fetch result into data or a thrown ApiError carrying the server's
 *  `detail`, which is what the UI shows the user. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || result.data === undefined) {
    throw new ApiError(result.response.status, detailOf(result.error, result.response));
  }
  return result.data;
}

function detailOf(error: unknown, response: Response): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d)))
        .join("; ");
    }
  }
  return `${response.status} ${response.statusText || "request failed"}`;
}
