"""Small capability-dependent entity construction contracts."""

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
