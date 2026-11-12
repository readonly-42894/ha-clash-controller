"""HA lifecycle and user-facing behavior, independent of the core implementation."""

import asyncio
import json
import logging
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from clash_controller_api import (
    APIAuthError,
    APIClientError,
    APIConnectionError,
    APITimeoutError,
    CapabilityReport,
    FetchResult,
    VersionInfo,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.clash_controller.const import DOMAIN
from custom_components.clash_controller.diagnostics import (
    async_get_config_entry_diagnostics,
)

HOST = "http://controller.local:9090/"
INPUT = {
    "api_url": HOST,
    "bearer_token": "secret-token",
    "allow_unsafe": False,
    "use_ssl": False,
}
PAYLOAD = {
    "traffic": {"up": 1048576, "down": 0},
    "connections": {"uploadTotal": 1073741824, "downloadTotal": 0, "connections": []},
    "memory": {"inuse": 1048576},
}


@pytest.fixture
def backend(monkeypatch):
    """Mock only the API boundary; HA setup, registries, entities and services are real."""
    instances = {}

    def factory(host, token, *args, **kwargs):
        if host not in instances:
            api = SimpleNamespace(
                capabilities={
                    **dict.fromkeys(PAYLOAD, True),
                    "ws_traffic": True,
                    "ws_memory": True,
                    "ws_connections": True,
                    "cache_fakeip_flush": True,
                    "cache_dns_flush": True,
                    "restart": True,
                },
                available_endpoints=[],
                payload=deepcopy(PAYLOAD),
                async_validate_connection=AsyncMock(return_value=None),
                async_get_version=AsyncMock(
                    return_value=VersionInfo(
                        model="Mihomo", version="test", meta="Meta Core"
                    )
                ),
                async_close=AsyncMock(),
                async_request=AsyncMock(return_value={}),
                log_queue=asyncio.Queue(),
                log_processed=asyncio.Event(),
                stream_queues={
                    key: asyncio.Queue() for key in PAYLOAD
                },
            )

            async def stream(key):
                while True:
                    frame = await api.stream_queues[key].get()
                    if frame is None:
                        return
                    yield deepcopy(frame)

            api.async_stream_traffic = lambda: stream("traffic")
            api.async_stream_memory = lambda: stream("memory")
            api.async_stream_connections = lambda: stream("connections")

            async def stream_logs(level: str = "warning"):
                while True:
                    entry = await api.log_queue.get()
                    if entry is None:
                        return
                    yield entry
                    api.log_processed.set()

            api.async_stream_logs = stream_logs

            async def fetch(*, skip=frozenset()):
                return FetchResult(
                    {
                        key: deepcopy(value)
                        for key, value in api.payload.items()
                        if key not in skip
                    },
                    {},
                )

            api.async_fetch_data = AsyncMock(side_effect=fetch)
            api.async_detect_capabilities = AsyncMock(
                return_value=CapabilityReport(dict(api.capabilities), {})
            )
            instances[host] = api
        return instances[host]

    monkeypatch.setattr(
        "custom_components.clash_controller.coordinator.ClashAPI", factory
    )
    monkeypatch.setattr(
        "custom_components.clash_controller.config_flow.ClashAPI", factory
    )
    return factory, instances


async def load_entry(hass, backend, host=HOST):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=host,
        data={**INPUT, "api_url": host},
        unique_id=host,
        options={"scan_interval": 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def entity_id(hass, entry, suffix):
    return next(
        item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.unique_id.endswith(suffix)
    )


async def poll(hass, seconds=11):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (APIAuthError("bad password"), "invalid_token"),
        (APIConnectionError("offline"), "cannot_connect"),
        (APITimeoutError("timeout"), "timed_out"),
        (APIClientError("malformed"), "cannot_connect"),
        (RuntimeError("unexpected failure"), "unknown"),
    ],
)
async def test_config_flow_errors(hass, backend, error, key, caplog):
    api = backend[0](HOST, "")
    api.async_validate_connection.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=dict(INPUT)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert not hass.config_entries.async_entries(DOMAIN)
    api.async_detect_capabilities.assert_not_awaited()
    unexpected = [
        record for record in caplog.records
        if record.message == "Unexpected error validating controller connection"
    ]
    assert len(unexpected) == (1 if key == "unknown" else 0)
    if unexpected:
        assert unexpected[0].exc_info is not None


async def test_flow_identity_reload_and_cleanup(hass, backend):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=dict(INPUT)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert "capabilities" not in entry.data
    assert "available_endpoints" not in entry.data
    api = backend[1][HOST]
    registry = er.async_get(hass)
    before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert (
        "http___controller_local_9090__device_traffic_sensor_upload_speed" in before
    )
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[
        0
    ]
    assert device.identifiers == {(DOMAIN, "http___controller_local_9090__device")}
    assert device.configuration_url == HOST
    assert device.model_id == DOMAIN
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=dict(INPUT)
    )
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    services = dict(hass.services.async_services_for_domain(DOMAIN))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert all(
        hass.services.async_services_for_domain(DOMAIN)[key] is value
        for key, value in services.items()
    )
    assert {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == before
    api.async_detect_capabilities.assert_awaited_with(force=True)
    api.async_fetch_data.reset_mock()
    api.async_validate_connection.reset_mock()
    api.async_get_version.reset_mock()
    api.async_detect_capabilities.reset_mock()
    await poll(hass, 61)
    api.async_fetch_data.assert_awaited_once()
    api.async_validate_connection.assert_not_awaited()
    api.async_get_version.assert_not_awaited()
    api.async_detect_capabilities.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    api.async_fetch_data.reset_mock()
    await poll(hass, 180)
    api.async_fetch_data.assert_not_awaited()
    assert hass.services.async_services_for_domain(DOMAIN)


async def test_entry_migration_removes_runtime_probe_data(hass, backend):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="controller",
        data={
            **INPUT,
            "capabilities": {"traffic": True},
            "available_endpoints": [["traffic", {"read_line": 1}]],
        },
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert "capabilities" not in entry.data
    assert "available_endpoints" not in entry.data
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("missing_endpoint", "missing_keys"),
    [
        ("providers_proxies", {"proxy_provider_count"}),
        ("providers_rules", {"rule_provider_count"}),
        (
            "all",
            {"upload_speed", "proxy_provider_count", "rule_provider_count"},
        ),
    ],
)
async def test_polling_outage_and_recovery(
    hass, backend, missing_endpoint, missing_keys, caplog
):
    caplog.set_level(logging.INFO, logger="custom_components.clash_controller.coordinator")
    api = backend[0](HOST, "")
    api.capabilities.update(providers_proxies=True, providers_rules=True)
    api.payload.update(
        providers_proxies={"providers": {"proxy": {}}},
        providers_rules={"providers": {"rule": {}}},
    )
    entry = await load_entry(hass, backend)
    entities = {
        "upload_speed": entity_id(hass, entry, "_upload_speed"),
        "proxy_provider_count": entity_id(hass, entry, "_proxy_provider_count"),
        "rule_provider_count": entity_id(hass, entry, "_rule_provider_count"),
    }
    original = deepcopy(api.payload)

    async def fetch(*, skip=frozenset()):
        return FetchResult(
            {
                key: deepcopy(value)
                for key, value in api.payload.items()
                if key not in skip
            },
            {
                key: APIConnectionError("offline")
                for key in original
                if key not in api.payload
            },
        )

    api.async_fetch_data.side_effect = fetch
    api.payload = (
        {}
        if missing_endpoint == "all"
        else {key: value for key, value in original.items() if key != missing_endpoint}
    )
    await poll(hass)
    assert entry.state is ConfigEntryState.LOADED
    for key, entity in entities.items():
        assert (hass.states.get(entity).state == STATE_UNAVAILABLE) is (
            key in missing_keys
        )
    await poll(hass, 22)
    endpoint_logs = [record.message for record in caplog.records if record.message.startswith("Endpoint ")]
    assert len(endpoint_logs) == (0 if missing_endpoint == "all" else 1)
    if endpoint_logs:
        assert f"Endpoint {missing_endpoint} unavailable" in endpoint_logs[0]
        assert "APIConnectionError" in endpoint_logs[0]
    api.payload = original
    await poll(hass, 33)
    assert entry.state is ConfigEntryState.LOADED
    for entity in entities.values():
        assert hass.states.get(entity).state != STATE_UNAVAILABLE
    endpoint_logs = [record.message for record in caplog.records if record.message.startswith("Endpoint ")]
    assert len(endpoint_logs) == (0 if missing_endpoint == "all" else 2)
    if endpoint_logs:
        assert f"Endpoint {missing_endpoint} recovered" in endpoint_logs[-1]


async def test_offline_start_retries_without_probing(hass, backend):
    api = backend[0](HOST, "")
    api.capabilities.update(
        ws_traffic=False, ws_memory=False, ws_connections=False
    )
    api.async_validate_connection.side_effect = APIConnectionError("offline")
    entry = MockConfigEntry(domain=DOMAIN, title="controller", data=dict(INPUT))
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    api.async_detect_capabilities.assert_not_awaited()
    api.async_fetch_data.assert_not_awaited()
    api.async_validate_connection.side_effect = None
    await poll(hass, 120)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


async def test_entities_added_after_first_refresh(hass, backend):
    api = backend[0](HOST, "")
    api.payload.pop("memory")
    entry = await load_entry(hass, backend)
    registry = er.async_get(hass)

    def registered():
        return {
            item.unique_id: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }

    before = registered()
    restored = deepcopy(PAYLOAD)
    api.payload = restored
    await poll(hass)
    after = registered()
    assert before.items() <= after.items()
    assert len(after) == len(before) + 1
    memory = entity_id(hass, entry, "_memory_used")
    assert hass.states.get(memory).state != STATE_UNAVAILABLE

    api.payload = {"traffic": PAYLOAD["traffic"]}
    await poll(hass, 22)
    assert hass.states.get(memory).state == STATE_UNAVAILABLE
    api.payload = restored
    await poll(hass, 33)
    assert registered() == after
    assert hass.states.get(memory).state != STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    api.async_fetch_data.reset_mock()
    await poll(hass, 120)
    api.async_fetch_data.assert_not_awaited()


async def test_stored_auth_failure_starts_reauthentication(hass, backend):
    api = backend[0](HOST, "")
    api.async_validate_connection.side_effect = APIAuthError("invalid")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="controller",
        data=dict(INPUT),
        unique_id=HOST,
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"

    api.async_validate_connection.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {"bearer_token": "replacement-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["bearer_token"] == "replacement-token"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_polling_auth_failure_starts_reauthentication(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    api.async_fetch_data.side_effect = None
    api.async_fetch_data.return_value = FetchResult(
        {}, {"traffic": APIAuthError("invalid")}
    )

    await poll(hass)

    assert entry.state is ConfigEntryState.LOADED
    upload = hass.states.get(entity_id(hass, entry, "_upload_speed"))
    assert upload.state == STATE_UNAVAILABLE
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_numeric_entities_and_missing_values(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    for suffix, value, unit, state_class in [
        ("_upload_speed", "1.048576", "MB/s", "measurement"),
        ("_download_speed", "0", "MB/s", "measurement"),
        ("_upload_traffic", "1.073741824", "GB", "total_increasing"),
        ("_memory_used", "1.048576", "MB", "measurement"),
    ]:
        state = hass.states.get(entity_id(hass, entry, suffix))
        assert float(state.state) == float(value)
        assert state.attributes["unit_of_measurement"] == unit
        assert state.attributes["state_class"] == state_class
    upload = entity_id(hass, entry, "_upload_speed")
    for offset, value in enumerate([None, True, "invalid", 0], 1):
        api.payload["traffic"]["up"] = value
        await poll(hass, 11 * offset)
        assert (hass.states.get(upload).state == STATE_UNAVAILABLE) is (value != 0)


async def test_websocket_push_updates_entities(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    upload = entity_id(hass, entry, "_upload_speed")
    assert float(hass.states.get(upload).state) == pytest.approx(1.048576)

    await api.stream_queues["traffic"].put({"up": 2097152, "down": 0})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert float(hass.states.get(upload).state) == pytest.approx(2.097152)


async def test_websocket_memory_placeholder_is_ignored(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    memory = entity_id(hass, entry, "_memory_used")

    await api.stream_queues["memory"].put({"inuse": 0})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert float(hass.states.get(memory).state) == pytest.approx(1.048576)


async def test_stream_disconnect_falls_back_to_polling(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    upload = entity_id(hass, entry, "_upload_speed")

    await api.stream_queues["traffic"].put({"up": 2097152, "down": 0})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert float(hass.states.get(upload).state) == pytest.approx(2.097152)

    api.payload["traffic"]["up"] = 3145728
    await api.stream_queues["traffic"].put(None)
    await hass.async_block_till_done()
    await poll(hass, 11)

    assert float(hass.states.get(upload).state) == pytest.approx(3.145728)


async def test_unload_stops_streams(hass, backend):
    entry = await load_entry(hass, backend)
    coordinator = entry.runtime_data.coordinator
    assert set(coordinator.stream_status) == {
        "traffic",
        "memory",
        "connections",
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator.stream_status == {}


async def test_options_update(hass, backend):
    entry = await load_entry(hass, backend)
    result = await hass.config_entries.options.async_init(
        entry.entry_id,
        data={"scan_interval": 20, "concurrent_connections": 3, "log_window": 600},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["bearer_token"] == INPUT["bearer_token"]
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=20)
    assert entry.runtime_data.coordinator.concurrent_connections == 3
    assert entry.runtime_data.coordinator.log_window == 600
    assert (
        hass.states.get(entity_id(hass, entry, "_upload_speed")).state
        != STATE_UNAVAILABLE
    )


async def test_diagnostics_redaction(hass, backend):
    entry = await load_entry(hass, backend)
    output = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for secret in [
        INPUT["bearer_token"],
        HOST,
        "controller.local",
    ]:
        assert secret not in output


@pytest.mark.parametrize(
    ("service", "data", "method", "endpoint", "reply", "expected", "error_key"),
    [
        ("reboot_core_service", {}, "POST", "restart", {}, None, "reboot_failed"),
        (
            "dns_query_service",
            {"domain_name": "example.com"},
            "GET",
            "dns/query",
            {"Answer": []},
            {"Answer": []},
            "dns_query_failed",
        ),
        (
            "get_rule_service",
            {"rule_payload": "example"},
            "GET",
            "rules",
            {"rules": [{"payload": "example.com"}, {"payload": "other"}]},
            {"rules": [{"payload": "example.com"}]},
            "rules_failed",
        ),
        (
            "api_call_service",
            {
                "api_endpoint": "configs",
                "api_method": "PATCH",
                "api_data": '{"mode":"global"}',
            },
            "PATCH",
            "configs",
            {"ok": True},
            {"response": {"ok": True}},
            "api_call_failed",
        ),
    ],
)
async def test_service_contracts(
    hass, backend, service, data, method, endpoint, reply, expected, error_key
):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    api.async_request.return_value = reply

    async def invoke():
        return await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": device.id, **data},
            blocking=True,
            return_response=service != "reboot_core_service",
        )

    assert await invoke() == expected
    call = api.async_request.await_args
    assert (call.args[0] if call.args else call.kwargs["method"]) == method
    assert (call.args[1] if call.args else call.kwargs["endpoint"]) == endpoint
    if service == "dns_query_service":
        assert call.kwargs["params"] == {"name": "example.com", "type": "A"}
    if service == "api_call_service":
        assert call.kwargs["json_data"] == {"mode": "global"}
    api.async_request.side_effect = APIConnectionError("offline")
    with pytest.raises(HomeAssistantError) as error:
        await invoke()
    assert error.value.translation_key == error_key
    assert entry.state is ConfigEntryState.LOADED


async def test_connections_and_multiple_entry_routing(hass, backend):
    first = await load_entry(hass, backend)
    second = await load_entry(hass, backend, "http://second.local/")
    api = backend[1]["http://second.local/"]
    device = dr.async_entries_for_config_entry(dr.async_get(hass), second.entry_id)[0]
    records = [
        {"id": "1", "metadata": {"host": "Example.COM"}},
        {"id": "2", "metadata": {"host": "other.com"}},
    ]
    api.async_request.return_value = {"connections": records}
    result = await hass.services.async_call(
        DOMAIN,
        "filter_connection_service",
        {"device_id": device.id, "host": "example", "close_connection": True},
        blocking=True,
        return_response=True,
    )
    assert result == {
        "connection_number": 1,
        "connection_closed": True,
        "connections": records[:1],
    }
    api.async_request.assert_any_await("DELETE", "connections/1")
    backend[1][HOST].async_request.assert_not_awaited()
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert second.state is ConfigEntryState.LOADED
    await hass.services.async_call(
        DOMAIN,
        "filter_connection_service",
        {"device_id": device.id, "close_connection": True},
        blocking=True,
        return_response=True,
    )
    api.async_request.assert_any_await("DELETE", "connections")


@pytest.mark.parametrize(
    ("service", "data", "key"),
    [
        (
            "api_call_service",
            {"api_method": "GET", "api_endpoint": "configs", "api_data": "[]"},
            "invalid_json",
        ),
        ("reboot_core_service", {}, "unsupported_action"),
        ("get_rule_service", {"device_id": "missing"}, "invalid_device"),
        ("get_rule_service", {"device_id": "foreign"}, "invalid_device"),
        ("get_rule_service", {"device_id": "unloaded"}, "invalid_device"),
    ],
)
async def test_service_validation(hass, backend, service, data, key):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    api.capabilities["restart"] = False
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    data = dict(data)
    if data.get("device_id") == "foreign":
        foreign_entry = MockConfigEntry(domain="other", state=ConfigEntryState.LOADED)
        foreign_entry.add_to_hass(hass)
        foreign_entry.runtime_data = entry.runtime_data
        foreign_device = dr.async_get(hass).async_get_or_create(
            config_entry_id=foreign_entry.entry_id,
            identifiers={("other", "device")},
        )
        data["device_id"] = foreign_device.id
    elif data.get("device_id") == "unloaded":
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        data["device_id"] = device.id
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": device.id, **data},
            blocking=True,
            return_response=service != "reboot_core_service",
        )
    assert error.value.translation_key == key
    api.async_request.assert_not_awaited()


async def test_button_writes(hass, backend):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, entry, "_flush_fakeip_cache")},
        blocking=True,
    )
    api.async_request.assert_awaited_once_with("POST", "cache/fakeip/flush")
    api.async_request.reset_mock()
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, entry, "_flush_dns_cache")},
        blocking=True,
    )
    api.async_request.assert_awaited_once_with("POST", "cache/dns/flush")


@pytest.mark.parametrize(
    "api_error",
    [APIConnectionError, APIAuthError, APITimeoutError, APIClientError],
)
async def test_button_action_errors_are_translated(hass, backend, api_error):
    entry = await load_entry(hass, backend)
    api = backend[1][HOST]
    api.async_request.side_effect = api_error("request failed")

    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, entry, "_flush_fakeip_cache")},
            blocking=True,
        )
    assert error.value.translation_key == "button_action_failed"


async def push_log(api, *levels):
    for level in levels:
        api.log_queue.put_nowait(SimpleNamespace(level=level, payload="test"))
        await asyncio.wait_for(api.log_processed.wait(), 2)
        api.log_processed.clear()


async def test_log_entities_require_capability(hass, backend):
    entry = await load_entry(hass, backend)
    registry = er.async_get(hass)
    assert not [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id.endswith(("_warning_log_count", "_error_log_count"))
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_log_counters_follow_window(hass, backend):
    api = backend[0](HOST, "")
    api.capabilities["ws_logs"] = True
    entry = await load_entry(hass, backend)
    coordinator = entry.runtime_data.coordinator
    warning = entity_id(hass, entry, "_warning_log_count")
    error = entity_id(hass, entry, "_error_log_count")
    assert hass.states.get(warning).state == "0"
    assert hass.states.get(error).state == "0"
    assert hass.states.get(warning).attributes["window_seconds"] == 300
    task = coordinator._log_stream_task
    assert task is not None

    await push_log(api, "warning", "warn", "error", "info", "debug")
    await poll(hass)
    assert hass.states.get(warning).state == "2"
    assert hass.states.get(error).state == "1"

    coordinator.log_window = 0
    await poll(hass)
    assert hass.states.get(warning).state == "0"
    assert hass.states.get(error).state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert task.cancelled()
