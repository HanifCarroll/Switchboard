from datetime import datetime, timezone

import pytest

from switchboard.configuration import deepseek_api_key, runtime_settings
from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.integrations import delivery_service
from tests.storage_fake import MemoryWorkspaceStore


@pytest.fixture(autouse=True)
def fresh_configuration():
    runtime_settings.cache_clear()
    deepseek_api_key.cache_clear()
    yield
    runtime_settings.cache_clear()
    deepseek_api_key.cache_clear()


@pytest.fixture
def delivered_delivery(monkeypatch):
    def delivered(*, destination, test_event_id, storage):
        return delivery_service.DeliveryTestResult(
            outcome="delivered",
            test_event_id=test_event_id,
            evidence=f"Domain test receiver accepted the event at {destination}",
        )

    monkeypatch.setattr(delivery_service, "send_synthetic_test_event", delivered)


@pytest.fixture
def memory_store() -> MemoryWorkspaceStore:
    return MemoryWorkspaceStore()


@pytest.fixture
def storage(memory_store: MemoryWorkspaceStore):
    storage = memory_store.storage(workspace_id="workspace-one")
    storage.reset_workspace(
        payload=build_workspace_payload(
            scenario_id="baseline",
            selected_scenario=load_scenarios()["baseline"],
            identity_mode="eval",
            updated_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
    )
    return storage
