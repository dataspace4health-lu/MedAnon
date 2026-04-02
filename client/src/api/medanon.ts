/**
 * Barrel re-export — all MedAnon API functions available from a single import.
 *
 * Domain-specific modules:
 *   ./health     — liveness & readiness probes
 *   ./configs    — config profile CRUD
 *   ./processing — FHIR de-identification (raw, batch, $everything)
 *   ./jobs       — async job queue (submit, poll, cancel, reprocess)
 *   ./upload     — upload to target FHIR server
 *   ./analytics  — risk assessment & synthetic data generation
 */

export * from "./health";
export * from "./configs";
export * from "./processing";
export * from "./jobs";
export * from "./upload";
export * from "./analytics";
