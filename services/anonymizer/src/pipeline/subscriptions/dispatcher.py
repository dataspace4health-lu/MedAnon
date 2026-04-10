"""Dispatches FHIR Subscriptions after successful resource processing.

Called from api/services/processing.py after process_data() returns.
Matches active subscriptions against the processed resource type and
delivers rest-hook webhooks asynchronously (fire-and-forget).
"""

import json
import logging
import os
import urllib.error
import urllib.request
import uuid as _uuid
from typing import Any

_dispatch_log = logging.getLogger("medanon.subscriptions")
_WEBHOOK_TIMEOUT = int(os.environ.get("SUBSCRIPTION_WEBHOOK_TIMEOUT", "10"))


def _extract_resource_type(resource: Any) -> str | None:
    """Extract resourceType from a processed resource (dict or list)."""
    if isinstance(resource, dict):
        return resource.get("resourceType")
    return None


def _matches_criteria(subscription: dict, resource_type: str | None) -> bool:
    """Return True if the subscription criteria matches this resource type.

    Supports simple criteria formats:
    - "Patient" or "Patient?" — matches resourceType == "Patient"
    - "Observation?category=vital-signs" — matches resourceType == "Observation" (type-only match)
    - "http://hl7.org/fhir/StructureDefinition/Patient" — profile URL (not supported, returns False)
    """
    if not resource_type:
        return False
    criteria = subscription.get("criteria", "")
    if not criteria:
        return False
    # Extract resource type from criteria (everything before '?' or end of string)
    criteria_type = criteria.split("?")[0].strip()
    # Profile URLs are not currently supported
    if criteria_type.startswith("http"):
        return False
    return criteria_type == resource_type


def _build_notification_bundle(resource: dict) -> str:
    """Build a minimal FHIR R4 Subscription notification bundle (history type)."""
    bundle = {
        "resourceType": "Bundle",
        "id": str(_uuid.uuid4()),
        "type": "history",
        "entry": [
            {
                "resource": resource,
                "request": {
                    "method": "PUT",
                    "url": "{}/{}".format(
                        resource.get("resourceType", "Resource"),
                        resource.get("id", ""),
                    ),
                },
            }
        ],
    }
    return json.dumps(bundle)


def _deliver_webhook(subscription: dict, resource: dict) -> None:
    """Synchronously deliver a webhook to the subscription endpoint.

    Any failure is logged but never re-raised — subscription delivery
    failures must not affect the primary de-identification response.
    """
    endpoint = subscription.get("channel", {}).get("endpoint", "")
    if not endpoint:
        return

    try:
        payload = _build_notification_bundle(resource).encode("utf-8")
    except Exception as exc:
        _dispatch_log.warning(
            "subscription_build_failed sub=%s: %s", subscription.get("id"), exc
        )
        return

    # Base headers plus any subscription-defined headers
    headers: dict[str, str] = {
        "Content-Type": "application/fhir+json",
        "X-Hub-Topic": subscription.get("criteria", ""),
    }
    for header_str in subscription.get("channel", {}).get("header", []):
        if ":" in header_str:
            k, _, v = header_str.partition(":")
            headers[k.strip()] = v.strip()

    try:
        req = urllib.request.Request(
            url=endpoint, data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT):  # nosec B310
            pass  # Success — response body not consumed
        _dispatch_log.info(
            "subscription_delivered sub=%s endpoint=%s",
            subscription.get("id"),
            endpoint,
        )
    except urllib.error.HTTPError as exc:
        _dispatch_log.warning(
            "subscription_webhook_http_error sub=%s status=%d endpoint=%s",
            subscription.get("id"),
            exc.code,
            endpoint,
        )
    except Exception as exc:
        _dispatch_log.warning(
            "subscription_webhook_failed sub=%s endpoint=%s error=%s",
            subscription.get("id"),
            endpoint,
            type(exc).__name__,
        )


def dispatch_subscriptions(resource: Any) -> None:
    """Dispatch subscriptions matching the given resource.

    Called after successful processing. Never raises — all errors are logged only.
    This is designed to be a best-effort, fire-and-forget notification.
    """
    from pipeline.subscriptions import get_subscription_store

    try:
        store = get_subscription_store()
        if store is None:
            return

        resource_type = _extract_resource_type(resource)
        if not resource_type:
            return

        active_subs = store.list_active()
        if not active_subs:
            return

        matching = [s for s in active_subs if _matches_criteria(s, resource_type)]
        for sub in matching:
            _deliver_webhook(sub, resource)
    except Exception as exc:
        _dispatch_log.warning("subscription_dispatch_error: %s", type(exc).__name__)
