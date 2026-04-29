/**
 * Auto-generated OpenAPI types live in this folder.
 *
 * Run ``npm run generate:api`` against a running MedAnon instance (or override
 * the URL with ``MEDANON_OPENAPI_URL``) to regenerate ``schema.d.ts``.
 *
 * Re-export helpers below provide ergonomic typed access from the rest of the
 * app without each call site needing to spell out the deeply-nested
 * ``paths['/v1/...']['get']['responses']['200']...`` chain.
 */

import type { paths, components } from "./schema";

// ---------------------------------------------------------------------------
// Schema helpers
// ---------------------------------------------------------------------------

/** Pydantic schema by name, e.g. ``Schema<'DashboardSummaryResponse'>``. */
export type Schema<K extends keyof components["schemas"]> =
  components["schemas"][K];

/** Successful JSON response body for ``GET <path>``. */
export type GetResponse<P extends keyof paths> =
  paths[P] extends { get: { responses: { 200: { content: { "application/json": infer T } } } } }
    ? T
    : never;

/** Successful JSON response body for ``POST <path>``. */
export type PostResponse<P extends keyof paths> =
  paths[P] extends {
    post: { responses: { 200: { content: { "application/json": infer T } } } };
  }
    ? T
    : paths[P] extends {
        post: { responses: { 202: { content: { "application/json": infer T } } } };
      }
    ? T
    : never;

/** JSON request body for ``POST <path>``. */
export type PostRequest<P extends keyof paths> =
  paths[P] extends {
    post: { requestBody: { content: { "application/json": infer T } } };
  }
    ? T
    : never;

export type { paths, components };
