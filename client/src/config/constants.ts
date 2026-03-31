/**
 * Application-wide constants for the SPE FHIR BlackBox React frontend.
 */

export const CONFIG_PROFILES = [
  { key: "auto", label: "Auto", description: "gpas if GPAS_URL is set, else minimal" },
  { key: "minimal", label: "Minimal", description: "Crypto hash + regex scrubbing" },
  { key: "gpas", label: "gPAS", description: "gPAS pseudonymization + generalization" },
  { key: "gdpr", label: "GDPR", description: "GDPR Art. 4(5) HMAC pseudonymization" },
  { key: "hipaa", label: "HIPAA", description: "HIPAA Safe Harbor (45 CFR \u00a7164.514)" },
  { key: "research", label: "Research", description: "IRB-grade research profile" },
  { key: "structural", label: "Structural", description: "Structure-preserving de-identification" },
] as const;

export type ConfigProfileKey = (typeof CONFIG_PROFILES)[number]["key"];

export const ROLE_HIERARCHY = ["viewer", "analyst", "admin"] as const;
export type Role = (typeof ROLE_HIERARCHY)[number];

/** Maximum upload body size in bytes (matches MEDANON_MAX_BODY_BYTES default). */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export const STORAGE_KEYS = {
  API_KEY: "medanon_api_key",
  CONFIG_PROFILE: "medanon_config_profile",
} as const;
