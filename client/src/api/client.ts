/**
 * Base fetch wrappers for the MedAnon API (/api) and FHIR server (/fhir).
 *
 * In production, nginx proxies /api -> MedAnon and /fhir -> HAPI FHIR.
 * In Vite dev mode, vite.config.ts proxies these paths to localhost services.
 */

import { ApiError } from "./types";

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

const API_KEY_STORAGE_KEY = "medanon_api_key";

function getApiKey(): string | null {
  try {
    return localStorage.getItem(API_KEY_STORAGE_KEY);
  } catch {
    // localStorage may be unavailable (SSR, iframe sandbox, etc.)
    return null;
  }
}

export function getAuthHeaders(): Record<string, string> {
  const key = getApiKey();
  if (key) {
    return { "X-API-Key": key };
  }
  return {};
}

// ---------------------------------------------------------------------------
// MedAnon API fetch wrapper
// ---------------------------------------------------------------------------

interface FetchApiOptions extends Omit<RequestInit, "signal"> {
  /** Request timeout in milliseconds (default: 60 000). */
  timeout?: number;
  /** External AbortSignal for cancellation. */
  signal?: AbortSignal | null;
}

/**
 * Typed fetch wrapper for the MedAnon REST API.
 *
 * - Prefixes `path` with `/api`
 * - Injects the API key header when configured
 * - Enforces a timeout via AbortController
 * - Parses JSON and throws `ApiError` on non-2xx status
 */
export async function fetchApi<T>(
  path: string,
  options: FetchApiOptions = {},
): Promise<T> {
  const { timeout = 60_000, signal: externalSignal, headers, ...rest } = options;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  // Combine external signal with timeout signal
  if (externalSignal) {
    externalSignal.addEventListener("abort", () => controller.abort(), {
      once: true,
    });
  }

  const url = `/api${path}`;

  try {
    const response = await fetch(url, {
      ...rest,
      signal: controller.signal,
      headers: {
        ...getAuthHeaders(),
        ...headers,
      },
    });

    if (!response.ok) {
      let body: unknown;
      let detail: string;
      try {
        body = await response.json();
        detail =
          typeof body === "object" && body !== null && "detail" in body
            ? String((body as Record<string, unknown>).detail)
            : response.statusText;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(response.status, detail, body);
    }

    return (await response.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

// ---------------------------------------------------------------------------
// FHIR server fetch wrapper
// ---------------------------------------------------------------------------

/**
 * Fetch a FHIR bundle from an absolute HAPI URL returned in a bundle link.
 *
 * HAPI returns paginated `next` links as full absolute URLs pointing at the
 * internal Docker host (e.g. http://hapi-fhir:8080/fhir?_getpages=...).
 * We strip the origin so the request goes through the nginx /fhir proxy.
 */
export async function fetchFhirByUrl<T>(hapiAbsoluteUrl: string): Promise<T> {
  // Extract pathname + search from the absolute URL so nginx can proxy it.
  let proxyUrl: string;
  try {
    const parsed = new URL(hapiAbsoluteUrl);
    proxyUrl = parsed.pathname + parsed.search;
  } catch {
    proxyUrl = hapiAbsoluteUrl;
  }

  const timeout = 15_000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(proxyUrl, {
      signal: controller.signal,
      headers: { Accept: "application/fhir+json" },
    });

    if (!response.ok) {
      let detail: string;
      try {
        const body = await response.json();
        detail = body?.issue?.[0]?.diagnostics ?? body?.detail ?? response.statusText;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(response.status, detail);
    }

    return (await response.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Typed fetch wrapper for the HAPI FHIR server.
 *
 * - Prefixes `path` with `/fhir`
 * - Sets Accept: application/fhir+json
 * - No auth header (FHIR server is internal)
 * - 15 s timeout by default
 */
export async function fetchFhir<T>(
  path: string,
  params?: Record<string, string>,
): Promise<T> {
  const timeout = 15_000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  let url = `/fhir${path}`;
  if (params) {
    const searchParams = new URLSearchParams(params);
    url += `?${searchParams.toString()}`;
  }

  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        Accept: "application/fhir+json",
      },
    });

    if (!response.ok) {
      let detail: string;
      try {
        const body = await response.json();
        detail =
          body?.issue?.[0]?.diagnostics ?? body?.detail ?? response.statusText;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(response.status, detail);
    }

    return (await response.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Typed fetch wrapper for the de-identified HAPI FHIR target server.
 *
 * - Prefixes `path` with `/fhir-target`
 * - Sets Accept: application/fhir+json
 * - 15 s timeout by default
 */
export async function fetchFhirTarget<T>(
  path: string,
  params?: Record<string, string>,
): Promise<T> {
  const timeout = 15_000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  let url = `/fhir-target${path}`;
  if (params) {
    const searchParams = new URLSearchParams(params);
    url += `?${searchParams.toString()}`;
  }

  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        Accept: "application/fhir+json",
      },
    });

    if (!response.ok) {
      let detail: string;
      try {
        const body = await response.json();
        detail =
          body?.issue?.[0]?.diagnostics ?? body?.detail ?? response.statusText;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(response.status, detail);
    }

    return (await response.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}
