"""Tests for FHIR R4 Subscription store, service, and dispatcher."""
import json
import tempfile
from unittest.mock import patch

import pytest

from pipeline.subscriptions import (
    SqliteSubscriptionStore,
    init_subscription_store,
    get_subscription_store,
)
from api.services.subscriptions import SubscriptionService, SubscriptionValidationError
from pipeline.subscriptions.dispatcher import (
    _matches_criteria,
    _build_notification_bundle,
    dispatch_subscriptions,
)

_VALID_SUB = {
    "resourceType": "Subscription",
    "status": "active",
    "criteria": "Patient",
    "channel": {
        "type": "rest-hook",
        "endpoint": "https://example.com/webhook",
        "header": ["Authorization: Bearer secret"],
    },
}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_subs.db")


@pytest.fixture
def store(db_path):
    return SqliteSubscriptionStore(db_path)


# ---------------------------------------------------------------------------
# SqliteSubscriptionStore
# ---------------------------------------------------------------------------

class TestSqliteSubscriptionStore:
    def test_create_and_get(self, store):
        sub = store.create(dict(_VALID_SUB))
        assert sub["id"]
        retrieved = store.get(sub["id"])
        assert retrieved["id"] == sub["id"]
        assert retrieved["criteria"] == "Patient"

    def test_create_generates_id(self, store):
        sub = store.create(
            {"criteria": "Patient", "channel": {"type": "rest-hook", "endpoint": "https://x.com/wh"}}
        )
        assert sub["id"]

    def test_create_preserves_existing_id(self, store):
        sub_in = {**_VALID_SUB, "id": "fixed-id-123"}
        sub = store.create(sub_in)
        assert sub["id"] == "fixed-id-123"

    def test_get_not_found(self, store):
        assert store.get("does-not-exist") is None

    def test_update(self, store):
        sub = store.create(dict(_VALID_SUB))
        updated = store.update(sub["id"], {**_VALID_SUB, "criteria": "Observation"})
        assert updated["criteria"] == "Observation"
        assert store.get(sub["id"])["criteria"] == "Observation"

    def test_update_not_found(self, store):
        assert store.update("ghost-id", _VALID_SUB) is None

    def test_delete(self, store):
        sub = store.create(dict(_VALID_SUB))
        assert store.delete(sub["id"]) is True
        assert store.get(sub["id"]) is None

    def test_delete_not_found(self, store):
        assert store.delete("ghost-id") is False

    def test_list_active_filters_inactive(self, store):
        store.create(dict(_VALID_SUB))
        store.create({**_VALID_SUB, "status": "off"})
        active = store.list_active()
        assert len(active) == 1
        assert active[0]["status"] == "active"

    def test_list_all_returns_all(self, store):
        store.create(dict(_VALID_SUB))
        store.create(dict(_VALID_SUB))
        assert len(store.list_all()) == 2

    def test_update_roundtrip_headers(self, store):
        """Headers must survive a create → get roundtrip."""
        sub = store.create(dict(_VALID_SUB))
        retrieved = store.get(sub["id"])
        assert retrieved["channel"]["header"] == ["Authorization: Bearer secret"]


# ---------------------------------------------------------------------------
# SubscriptionService validation
# ---------------------------------------------------------------------------

class TestSubscriptionService:
    @pytest.fixture
    def service(self, db_path):
        init_subscription_store(db_path)
        return SubscriptionService()

    def test_create_valid(self, service):
        result = service.create(dict(_VALID_SUB))
        assert result["id"]

    def test_create_invalid_channel_type(self, service):
        sub = {**_VALID_SUB, "channel": {**_VALID_SUB["channel"], "type": "websocket"}}
        with pytest.raises(SubscriptionValidationError, match="Unsupported channel type"):
            service.create(sub)

    def test_create_missing_endpoint(self, service):
        sub = {**_VALID_SUB, "channel": {"type": "rest-hook"}}
        with pytest.raises(SubscriptionValidationError, match="endpoint"):
            service.create(sub)

    def test_create_missing_criteria(self, service):
        sub = {k: v for k, v in _VALID_SUB.items() if k != "criteria"}
        with pytest.raises(SubscriptionValidationError, match="criteria"):
            service.create(sub)

    def test_create_non_http_endpoint(self, service):
        bad_channel = {**_VALID_SUB["channel"], "endpoint": "ftp://bad.com"}
        with pytest.raises(SubscriptionValidationError, match="http"):
            service.create({**_VALID_SUB, "channel": bad_channel})

    def test_create_wrong_resource_type(self, service):
        with pytest.raises(SubscriptionValidationError, match="resourceType"):
            service.create({**_VALID_SUB, "resourceType": "Patient"})

    def test_get_existing(self, service):
        created = service.create(dict(_VALID_SUB))
        assert service.get(created["id"]) is not None

    def test_get_not_found(self, service):
        assert service.get("nope") is None

    def test_update_not_found(self, service):
        assert service.update("nope", _VALID_SUB) is None

    def test_delete(self, service):
        created = service.create(dict(_VALID_SUB))
        assert service.delete(created["id"]) is True

    def test_list_all(self, service):
        service.create(dict(_VALID_SUB))
        service.create(dict(_VALID_SUB))
        assert len(service.list_all()) == 2

    def test_store_not_initialised_raises(self):
        import pipeline.subscriptions as sub_mod
        original = sub_mod._sub_store
        sub_mod._sub_store = None
        try:
            svc = SubscriptionService()
            with pytest.raises(RuntimeError, match="not initialised"):
                svc.list_all()
        finally:
            sub_mod._sub_store = original


# ---------------------------------------------------------------------------
# Subscription dispatcher
# ---------------------------------------------------------------------------

class TestMatchesCriteria:
    def test_exact_match(self):
        assert _matches_criteria({"criteria": "Patient"}, "Patient") is True

    def test_query_params_stripped(self):
        assert _matches_criteria({"criteria": "Observation?category=vital-signs"}, "Observation") is True

    def test_no_match(self):
        assert _matches_criteria({"criteria": "Patient"}, "Observation") is False

    def test_profile_url_not_supported(self):
        assert _matches_criteria(
            {"criteria": "http://hl7.org/fhir/StructureDefinition/Patient"}, "Patient"
        ) is False

    def test_no_resource_type(self):
        assert _matches_criteria({"criteria": "Patient"}, None) is False

    def test_empty_criteria(self):
        assert _matches_criteria({"criteria": ""}, "Patient") is False


class TestBuildNotificationBundle:
    def test_bundle_structure(self):
        resource = {"resourceType": "Patient", "id": "p1"}
        bundle = json.loads(_build_notification_bundle(resource))
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "history"
        assert bundle["entry"][0]["resource"] == resource

    def test_unique_bundle_ids(self):
        resource = {"resourceType": "Patient", "id": "p1"}
        b1 = json.loads(_build_notification_bundle(resource))
        b2 = json.loads(_build_notification_bundle(resource))
        assert b1["id"] != b2["id"]


class TestDispatchSubscriptions:
    def test_no_store_is_noop(self):
        import pipeline.subscriptions as sub_mod
        original = sub_mod._sub_store
        sub_mod._sub_store = None
        try:
            dispatch_subscriptions({"resourceType": "Patient", "id": "p1"})
        finally:
            sub_mod._sub_store = original

    def test_no_matching_subscriptions_no_call(self, db_path):
        init_subscription_store(db_path)
        with patch("urllib.request.urlopen") as mock_open:
            dispatch_subscriptions({"resourceType": "Patient", "id": "p1"})
        mock_open.assert_not_called()

    def test_delivers_matching_webhook(self, db_path):
        store = init_subscription_store(db_path)
        store.create(dict(_VALID_SUB))

        class _MockResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=_MockResp()) as mock_open:
            dispatch_subscriptions({"resourceType": "Patient", "id": "p1"})
        mock_open.assert_called_once()

    def test_skips_non_matching_resource_type(self, db_path):
        store = init_subscription_store(db_path)
        store.create(dict(_VALID_SUB))  # criteria = "Patient"
        with patch("urllib.request.urlopen") as mock_open:
            dispatch_subscriptions({"resourceType": "Observation", "id": "obs1"})
        mock_open.assert_not_called()

    def test_swallows_webhook_network_failure(self, db_path):
        store = init_subscription_store(db_path)
        store.create(dict(_VALID_SUB))
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            dispatch_subscriptions({"resourceType": "Patient", "id": "p1"})

    def test_non_dict_resource_is_ignored(self, db_path):
        init_subscription_store(db_path)
        dispatch_subscriptions(["not", "a", "dict"])

    def test_custom_headers_forwarded(self, db_path):
        """Channel headers must be sent in the webhook request."""
        store = init_subscription_store(db_path)
        store.create(dict(_VALID_SUB))  # has Authorization header

        captured: list = []

        class _MockResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_urlopen(req, timeout=None):
            captured.append(req)
            return _MockResp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            dispatch_subscriptions({"resourceType": "Patient", "id": "p1"})

        assert captured
        assert captured[0].get_header("Authorization") == "Bearer secret"
