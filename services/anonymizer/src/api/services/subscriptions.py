"""Subscription management service.

Validates and delegates CRUD operations to the module-level
SqliteSubscriptionStore. Only 'rest-hook' channel type is accepted.
"""

import logging
import urllib.parse
from typing import Optional

logger = logging.getLogger("medanon")


class SubscriptionValidationError(Exception):
    """Raised when a Subscription resource fails business-rule validation."""


class SubscriptionService:
    SUPPORTED_CHANNEL_TYPES = {"rest-hook"}

    def _get_store(self):
        from pipeline.subscriptions import get_subscription_store

        store = get_subscription_store()
        if store is None:
            raise RuntimeError("Subscription store not initialised")
        return store

    def _validate(self, sub: dict) -> None:
        """Raise SubscriptionValidationError if the subscription body is invalid."""
        if sub.get("resourceType") not in (None, "Subscription"):
            raise SubscriptionValidationError("resourceType must be 'Subscription'")
        channel = sub.get("channel", {})
        if not channel:
            raise SubscriptionValidationError("channel is required")
        channel_type = channel.get("type")
        if channel_type not in self.SUPPORTED_CHANNEL_TYPES:
            raise SubscriptionValidationError(
                f"Unsupported channel type '{channel_type}'. "
                f"Only {self.SUPPORTED_CHANNEL_TYPES} is supported."
            )
        if not channel.get("endpoint"):
            raise SubscriptionValidationError("channel.endpoint is required")
        if not sub.get("criteria"):
            raise SubscriptionValidationError("criteria is required")
        endpoint = channel.get("endpoint", "")
        if not endpoint.startswith(("http://", "https://")):
            raise SubscriptionValidationError("channel.endpoint must be an http(s) URL")
        # SSRF protection: resolve hostname and reject private/loopback addresses
        parsed = urllib.parse.urlparse(endpoint)
        hostname = parsed.hostname or ""
        if hostname:
            from utils.ssrf import check_hostname_ssrf

            ssrf_error = check_hostname_ssrf(hostname)
            if ssrf_error:
                raise SubscriptionValidationError(
                    f"channel.endpoint rejected: {ssrf_error}"
                )

    def create(self, sub: dict) -> dict:
        """Validate and persist a new subscription."""
        self._validate(sub)
        return self._get_store().create(sub)

    def get(self, sub_id: str) -> Optional[dict]:
        """Return a subscription by id, or None if not found."""
        return self._get_store().get(sub_id)

    def update(self, sub_id: str, sub: dict) -> Optional[dict]:
        """Validate and replace a subscription. Returns None if not found."""
        self._validate(sub)
        return self._get_store().update(sub_id, sub)

    def delete(self, sub_id: str) -> bool:
        """Delete a subscription. Returns True if it existed."""
        return self._get_store().delete(sub_id)

    def list_all(self) -> list[dict]:
        """Return all subscriptions (any status), newest first."""
        return self._get_store().list_all()
