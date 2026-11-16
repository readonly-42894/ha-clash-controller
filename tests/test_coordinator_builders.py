"""Small capability-dependent entity construction contracts."""

from collections import deque
from types import SimpleNamespace

from homeassistant.helpers.entity import EntityCategory

from custom_components.clash_controller.coordinator import ClashControllerCoordinator


def test_provider_entities():
    coordinator = object.__new__(ClashControllerCoordinator)
    entities = coordinator._build_provider_entities(
        {"providers": {"default": {}, "HK Group": {"timeout": "3000"}}},
        {"providers": {"rules": {}}},
    )
    counts = {
        item.unique_key: item.state
        for item in entities
        if item.entity_type == "provider_count_sensor"
    }
    assert counts == {"proxy_provider_count": 2, "rule_provider_count": 1}
    assert [
        item for item in entities if item.entity_type != "provider_count_sensor"
    ] == []

    for absent in (None, {}, {"providers": []}):
        entities = coordinator._build_provider_entities({"providers": {}}, absent)
        assert [(item.unique_key, item.state) for item in entities] == [
            ("proxy_provider_count", 0)
        ]
        entities = coordinator._build_provider_entities(absent, {"providers": {}})
        assert [(item.unique_key, item.state) for item in entities] == [
            ("rule_provider_count", 0)
        ]


def test_log_events_prune_outside_window():
    coordinator = object.__new__(ClashControllerCoordinator)
    coordinator.log_window = 300
    coordinator._log_events = {
        "warning": deque([100.0, 400.0, 700.001]),
        "error": deque([250.0]),
    }

    coordinator._prune_log_events(now=1000.0)

    assert list(coordinator._log_events["warning"]) == [700.001]
    assert list(coordinator._log_events["error"]) == []


def test_record_log_normalizes_levels():
    coordinator = object.__new__(ClashControllerCoordinator)
    coordinator.log_window = 3600
    coordinator._log_events = {"warning": deque(), "error": deque()}

    for level in ("warn", "WARNING", "error", "info", "debug"):
        coordinator._record_log(level)

    assert len(coordinator._log_events["warning"]) == 2
    assert len(coordinator._log_events["error"]) == 1


def test_smart_flush_button_targets_global_smart_cache():
    coordinator = object.__new__(ClashControllerCoordinator)
    coordinator.api = SimpleNamespace(async_request=object())
    button = coordinator._build_smart_flush_button()

    assert button.entity_type == "smart_flush_button"
    assert button.unique_key == "flush_smart_weights"
    assert button.translation_key == "flush_smart_weights"
    assert button.entity_category is EntityCategory.DIAGNOSTIC
    assert button.action["method"] is coordinator.api.async_request
    assert button.action["args"] == ("POST", "cache/smart/flush")
    assert button.action["kwargs"] == {}
