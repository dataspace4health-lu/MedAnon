/**
 * Placeholder for the OpenAPI-generated schema.
 *
 * This file is overwritten by ``npm run generate:api`` against a running
 * MedAnon instance.  Until then it exposes a minimal ``paths`` /
 * ``components`` shape so the typed re-exports in ``./index.ts`` compile.
 *
 * DO NOT hand-edit; changes will be lost on next regeneration.
 */

export interface paths {}

export interface components {
  schemas: Record<string, unknown>;
}

export type webhooks = Record<string, never>;
export type operations = Record<string, never>;
