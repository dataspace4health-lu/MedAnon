"""Pydantic models mirroring the NLP microservice API contract.

These schemas validate responses from the NLP service so that field renames
or type changes are caught immediately rather than causing silent data loss.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class NlpDetectRequest(BaseModel):
    text: str
    entities: list[str] | str = "healthcare"
    threshold: float = 0.4
    language: str = "en"
    mode: str = "tokenize"
    token_state: dict[str, Any] | None = None
    detect_only: bool = False


class NlpDetectResponse(BaseModel):
    scrubbed_text: str | None = None
    token_state: dict[str, Any] = Field(default_factory=dict)
    detections: list | None = None


class NlpBatchItem(BaseModel):
    text: str
    entities: list[str] | str = "healthcare"
    threshold: float = 0.4
    language: str = "en"
    mode: str = "tokenize"
    detect_only: bool = False


class NlpBatchRequest(BaseModel):
    items: list[NlpBatchItem]
    token_state: dict[str, Any] | None = None


class NlpBatchResponse(BaseModel):
    results: list[str] = Field(default_factory=list)
    token_state: dict[str, Any] = Field(default_factory=dict)
    detections: list[list] | None = None
