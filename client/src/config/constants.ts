/**
 * Application-wide constants for the SPE FHIR BlackBox React frontend.
 */

export const ROLE_HIERARCHY = ["viewer", "analyst", "admin"] as const;
export type Role = (typeof ROLE_HIERARCHY)[number];

/** Maximum upload body size in bytes (matches MEDANON_MAX_BODY_BYTES default). */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export const STORAGE_KEYS = {
  API_KEY: "medanon_api_key",
  CONFIG_PROFILE: "medanon_config_profile",
  REFRESH_TOKEN: "medanon_refresh_token",
} as const;
