/**
 * NDJSON streaming support for the MedAnon batch/streaming endpoints.
 *
 * Uses the Streams API to read a Response body incrementally and yield
 * parsed JSON objects one line at a time. Handles partial line buffering
 * when a JSON line is split across ReadableStream chunks.
 */

import { getAuthHeaders } from "./client";

/**
 * Async generator that streams NDJSON from `url`.
 *
 * Each yielded value is a parsed JSON object from one line of the response.
 * Empty lines and comment lines (starting with "//") are silently skipped.
 *
 * @param url     - Full URL to fetch (e.g. `/api/process/batch?config_profile=auto`).
 * @param options - Fetch options (method, headers, body, etc.).
 * @param signal  - Optional AbortSignal for cancellation.
 */
export async function* streamNdjson<T = Record<string, unknown>>(
  url: string,
  options: RequestInit = {},
  signal?: AbortSignal,
): AsyncGenerator<T, void, undefined> {
  const response = await fetch(url, {
    ...options,
    signal,
    headers: {
      ...getAuthHeaders(),
      ...options.headers,
    },
  });

  if (!response.ok) {
    let detail: string;
    try {
      const body = await response.json();
      detail =
        typeof body === "object" && body !== null && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`Stream request failed (${response.status}): ${detail}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Response body is not readable");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (value) {
        buffer += decoder.decode(value, { stream: true });
      }

      // Process all complete lines currently in the buffer.
      // A complete line ends with '\n'. The last segment (after the final '\n')
      // may be a partial line that we keep in the buffer for the next chunk.
      let newlineIdx: number;
      while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIdx).trim();
        buffer = buffer.slice(newlineIdx + 1);

        // Skip empty lines and NDJSON comments
        if (!line || line.startsWith("//")) {
          continue;
        }

        try {
          yield JSON.parse(line) as T;
        } catch {
          // Malformed JSON line -- skip rather than abort the whole stream.
          // Callers can detect errors via the `error` field in parsed objects.
          console.warn("[streamNdjson] skipping unparseable line:", line);
        }
      }

      if (done) {
        break;
      }
    }

    // Flush any remaining data in the buffer after the stream ends.
    // This handles the case where the final line does not end with '\n'.
    const remaining = buffer.trim();
    if (remaining && !remaining.startsWith("//")) {
      try {
        yield JSON.parse(remaining) as T;
      } catch {
        console.warn(
          "[streamNdjson] skipping unparseable trailing data:",
          remaining,
        );
      }
    }
  } finally {
    reader.releaseLock();
  }
}
