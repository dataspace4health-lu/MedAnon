"""integrations.staging  PostgreSQL two-phase staging store.

The staging store buffers FHIR resources in ``medanon.staged_resources``
between Phase 1 (FHIR fetch) and Phase 2 (de-identification + write),
enabling overlap and crash recovery.  Uses ``FOR UPDATE SKIP LOCKED``
for safe concurrent partition claiming.
"""
