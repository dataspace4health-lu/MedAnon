"""Config Chat agent.

Conversational Q&A about FHIR de-identification configurations. Answers
questions like "why redact birthDate?", "what action fits an SSN?", or
"is this HIPAA Safe Harbor compliant?" with the current config as context.

Safe for any local LLM — config YAML contains rule definitions only, no PHI.
Streams chunks for a responsive UI; falls back to a static message when the
provider is unavailable.
"""

import logging
import re
from collections.abc import Generator

_log = logging.getLogger("medanon.ai.config_chat")

# Fixed refusal returned for off-topic requests (matches the system prompt).
_OFF_TOPIC_REPLY = (
    "I can only help with FHIR de-identification configuration — rules, "
    "actions, FHIRPath expressions, and compliance mapping. Please ask about "
    "your config."
)

# Vocabulary that signals a question is about de-identification config. Used as
# a cheap, deterministic on-topic gate BEFORE calling the model, so blatantly
# off-topic prompts ("write me a poem") never reach the LLM. This is a recall
# filter, not precision — anything plausibly on-topic passes through and the
# system-prompt scope rule does the finer enforcement.
_ON_TOPIC_TERMS = (
    "config",
    "configuration",
    "profile",
    "rule",
    "rules",
    "action",
    "redact",
    "hash",
    "cryptohash",
    "encrypt",
    "decrypt",
    "perturb",
    "substitute",
    "generalize",
    "generali",
    "scrub",
    "nlp",
    "pseudonym",
    "gpas",
    "fhir",
    "fhirpath",
    "match",
    "resource",
    "patient",
    "observation",
    "field",
    "path",
    "param",
    "deident",
    "de-ident",
    "anonym",
    "phi",
    "pii",
    "hipaa",
    "gdpr",
    "safe harbor",
    "compliance",
    "regulation",
    "mask",
    "identifier",
    "birthdate",
    "birth date",
    "date",
    "zip",
    "address",
    "name",
    "telecom",
    "ssn",
    "mrn",
    "medical record",
    "value",
    "yaml",
    "this",
)


def _is_on_topic(question: str) -> bool:
    """Cheap recall gate: does the question plausibly concern config?

    Returns True if any on-topic term appears (word-boundary match). Short
    follow-ups like "why?" or "and that one?" are allowed through because they
    rely on conversation context, which the model resolves.
    """
    q = question.lower().strip()
    if len(q.split()) <= 3:
        return True  # short follow-ups depend on prior turns
    return any(re.search(rf"\b{re.escape(t)}\b", q) for t in _ON_TOPIC_TERMS)


_CHAT_SYSTEM_PROMPT = """\
You are a healthcare data-privacy engineer helping a user build and reason \
about FHIR de-identification configuration profiles for the SPE FHIR BlackBox \
engine. Answer the user's questions clearly and concisely in markdown.

SCOPE:
Your job is to help with FHIR de-identification configuration. This INCLUDES \
anything about: which action to use for a field (redact, hash, encrypt, \
generalize, scrub, pseudonymize, etc.), FHIRPath match expressions, action \
parameters, choosing or comparing config profiles, what a rule does, whether a \
config is privacy-safe, and how rules map to HIPAA or GDPR. Answer all such \
questions fully and helpfully — this is your normal job, so do it.

ONLY refuse when the request is clearly unrelated to building a config — for \
example creative writing, general medical/clinical advice, unrelated coding \
help, or general trivia. In that narrow case, reply with EXACTLY this sentence \
and nothing else:
"I can only help with FHIR de-identification configuration — rules, actions, \
FHIRPath expressions, and compliance mapping. Please ask about your config."
When in doubt, assume the question IS about the config and answer it.

You understand these rule actions:
- redact: permanently remove the value (irreversible)
- cryptohash: one-way HMAC hash — preserves linkage within a dataset
- encrypt / decrypt: reversible with an RSA key — use when re-identification \
is required
- perturb: add bounded random noise to a numeric/date value
- substitute: replace with a fixed or mapped value
- generalize: reduce precision (e.g. exact date -> year, zip -> 3-digit prefix)
- scrub_text: regex-based text replacement in free-text fields
- nlp_scrub / nlp_detect_act: AI/NER-based PHI removal in free text
- gpas_pseudonymize: external TTP pseudonym service (reversible pseudonyms)

Rules match FHIR elements with FHIRPath expressions (e.g. ``Patient.name``,
``Observation.note.text``). A rule has: name, match (FHIRPath), action, and
optional action-specific params.

Regulatory anchors you can cite:
- HIPAA Safe Harbor 45 CFR 164.514(b): 18 PHI identifier categories; dates to \
year only; zip to 3-digit prefix.
- GDPR Art. 4(5): pseudonymization; Art. 89: research derogation.

Guidance:
- When the user shares a config, ground your answer in their actual rules.
- Recommend the most privacy-preserving action that still meets their stated \
use case; explain the trade-off briefly.
- If something in their config looks risky (e.g. free-text left unscrubbed, \
reversible action on a direct identifier), say so.
- Keep answers focused — a few short paragraphs or a tight list, not an essay.
"""


def _build_messages(
    question: str,
    config_yaml: str = "",
    history: list[dict] | None = None,
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    if config_yaml.strip():
        messages.append(
            {
                "role": "system",
                "content": (
                    "The user is currently editing this configuration:\n"
                    f"```yaml\n{config_yaml.strip()[:12000]}\n```"
                ),
            },
        )
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:8000]})
    messages.append({"role": "user", "content": question})
    return messages


def chat_config(
    question: str,
    *,
    config_yaml: str = "",
    history: list[dict] | None = None,
    model: str = "",
    streaming: bool = False,
):
    """Answer a config question.

    streaming=True -> generator of text chunks (for SSE).
    streaming=False -> full answer string.
    """
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    # Deterministic on-topic gate: blatantly off-topic prompts never reach the
    # model. The system-prompt scope rule is the second line of defence for
    # borderline cases that pass this filter.
    if not _is_on_topic(question):
        _log.info("config_chat_off_topic_blocked len=%d", len(question))
        return iter([_OFF_TOPIC_REPLY]) if streaming else _OFF_TOPIC_REPLY

    messages = _build_messages(question, config_yaml, history)
    model_override = model or None

    try:
        provider = get_provider()
    except NotAvailableError:
        msg = (
            "The AI assistant is disabled. Set `MEDANON_AI_ENABLED=true` and "
            "configure a local Ollama model to chat about your configuration."
        )
        return iter([msg]) if streaming else msg

    try:
        # Config questions + YAML rules only — no resource content. Note a
        # user-supplied model_override is still locality-checked when
        # MEDANON_AI_REQUIRE_LOCAL=true (site-wide hard lock).
        if streaming:
            return provider.complete_streaming(
                messages,
                model_override=model_override,
                temperature=0.3,
                phi_payload=False,
            )
        return provider.complete(
            messages,
            model_override=model_override,
            temperature=0.3,
            phi_payload=False,
        )
    except ProviderUnavailableError as exc:
        _log.info("config_chat_unavailable: %s", exc)
        msg = (
            "The AI model is currently unreachable. Check that Ollama is "
            "running and `MEDANON_AI_API_BASE` is correct, then try again."
        )
        return iter([msg]) if streaming else msg


def _is_generator(obj) -> bool:
    return isinstance(obj, Generator) or hasattr(obj, "__next__")
