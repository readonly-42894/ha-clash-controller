"""Data coordinator for Clash Controller."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from clash_controller_api import (
    APIAuthError,
    APIClientError,
    APIConnectionError,
    APITimeoutError,
    ClashAPI,
    ClashAPIError,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONCURRENT_CONNECTIONS,
    CONF_LOG_WINDOW,
    DEFAULT_CONCURRENT_CONNECTIONS,
    DEFAULT_LOG_WINDOW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOG_LEVELS,
    LOG_RECONNECT_INITIAL,
    LOG_RECONNECT_MAX,
    STREAM_ENDPOINT_CAPABILITIES,
    STREAM_PUBLISH_INTERVAL,
    STREAM_RECONNECT_INITIAL,
    STREAM_RECONNECT_MAX,
    STREAM_STALE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
CORE_DATA_KEYS = frozenset(
    {
        "traffic",
        "memory",
        "connections",
        "proxies",
        "configs",
        "providers_proxies",
        "providers_rules",
    }
)


def _device_id_from_host(host: str) -> str:
    """Return the legacy Home Assistant device identifier for a controller host."""
    return re.sub(r"[^a-zA-Z0-9]", "_", host.strip().lower().rstrip("_")) + "_device"


@dataclass(slots=True)
class ClashEntityData:
    """Structured data model used by entities."""

    name: str | None
    entity_type: str
    state: Any = None
    icon: str | None = None
    translation_key: str | None = None
    translation_placeholders: dict[str, str] | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool | None = None
    attributes: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    unique_key: str | None = None
    unique_id: str = ""
    device_info: DeviceInfo | None = None


class ClashControllerCoordinator(DataUpdateCoordinator[list[ClashEntityData]]):
    """A coordinator to fetch data from the Clash API."""

    device: DeviceInfo | None = None

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the Clash Controller coordinator."""
        self.host = config_entry.data["api_url"]
        self.token = config_entry.data["bearer_token"]
        self.allow_unsafe = config_entry.data["allow_unsafe"]
        self.device_id = _device_id_from_host(self.host)
        self.config_entry = config_entry

        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.concurrent_connections = config_entry.options.get(
            CONF_CONCURRENT_CONNECTIONS, DEFAULT_CONCURRENT_CONNECTIONS
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} ({self.host})",
            update_interval=timedelta(seconds=self.poll_interval),
        )

        self.api = ClashAPI(
            host=self.host,
            token=self.token,
            session=async_get_clientsession(hass, verify_ssl=not self.allow_unsafe),
        )
        self._data_by_unique_id: dict[str, ClashEntityData] = {}
        self._failed_endpoints: set[str] = set()
        self._latest: dict[str, Any] = {}
        self._stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._stream_last_frame: dict[str, float] = {}
        self._stream_publish_unsub: Callable[[], None] | None = None

        self.log_window = config_entry.options.get(CONF_LOG_WINDOW, DEFAULT_LOG_WINDOW)
        self._log_events: dict[str, deque[float]] = {
            level: deque() for level in LOG_LEVELS
        }
        self._log_stream_task: asyncio.Task[None] | None = None

    async def _async_setup(self) -> None:
        """Load data that remains stable for this coordinator instance."""
        try:
            await self.api.async_validate_connection()
        except APIAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (APIClientError, APIConnectionError, APITimeoutError) as err:
            raise UpdateFailed(err) from err
        await self.api.async_detect_capabilities(force=True)
        self.device = await self._get_device()
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            **self.device,
        )

    async def _get_device(self) -> DeviceInfo:
        """Generate a device object."""
        version_info = await self.api.async_get_version()
        model = version_info.model
        lowered_model = model.lower()
        manufacturer = (
            "MetaCubeX"
            if ("mihomo" in lowered_model or "meta" in lowered_model)
            else "Clash"
        )
        return DeviceInfo(
            configuration_url=self.host,
            identifiers={(DOMAIN, self.device_id)},
            manufacturer=manufacturer,
            model=model,
            model_id=DOMAIN,
            sw_version=version_info.version,
            translation_key="clash_instance",
        )

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        skip = self._streaming_keys()
        capabilities = self.api.capabilities or {}
        requested = {
            key
            for key in CORE_DATA_KEYS
            if capabilities.get(key) and key not in skip
        }
        try:
            result = await self.api.async_fetch_data(skip=skip)
            response = result.data
            if any(
                isinstance(error, APIAuthError) for error in result.errors.values()
            ):
                raise ConfigEntryAuthFailed
            if result.errors and not response:
                raise UpdateFailed(next(iter(result.errors.values())))
            if not CORE_DATA_KEYS.intersection(response) and not skip:
                if result.errors:
                    raise UpdateFailed(next(iter(result.errors.values())))
                raise UpdateFailed("No data returned from Clash core.")
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            raise UpdateFailed(err) from err

        for key in requested - set(response):
            self._latest.pop(key, None)
        self._latest.update(response)
        data = self._build_entity_data(self._latest)
        real_entities = [
            item
            for item in data
            if item.entity_type not in {"fakeip_flush_button", "dns_flush_button"}
        ]
        if not real_entities:
            raise UpdateFailed("Empty response")

        failed_endpoints = set(result.errors)
        for endpoint in sorted(failed_endpoints - self._failed_endpoints):
            _LOGGER.info(
                "Endpoint %s unavailable for entry %s: %s",
                endpoint,
                self.config_entry.entry_id,
                type(result.errors[endpoint]).__name__,
            )
        if self.last_update_success:
            for endpoint in sorted(self._failed_endpoints.intersection(response)):
                _LOGGER.info(
                    "Endpoint %s recovered for entry %s",
                    endpoint,
                    self.config_entry.entry_id,
                )
        self._failed_endpoints = failed_endpoints

        return data

    def _streaming_keys(self) -> set[str]:
        """Return endpoints whose websocket stream delivered a fresh frame."""
        now = time.monotonic()
        return {
            key
            for key, stamp in self._stream_last_frame.items()
            if now - stamp <= STREAM_STALE_SECONDS
        }

    @property
    def stream_status(self) -> dict[str, dict[str, Any]]:
        """Return transport state for diagnostics."""
        now = time.monotonic()
        return {
            key: {
                "subscribed": True,
                "last_frame_age_seconds": (
                    round(now - self._stream_last_frame[key], 1)
                    if key in self._stream_last_frame
                    else None
                ),
            }
            for key in sorted(self._stream_tasks)
        }

    def async_start_streams(self) -> None:
        """Start the log and real-time websocket subscriptions."""
        capabilities = self.api.capabilities or {}
        if capabilities.get("ws_logs"):
            self.async_start_log_stream()
        if self._stream_tasks:
            return
        factories: dict[str, Callable[[], AsyncIterator[dict[str, Any]]]] = {
            "traffic": self.api.async_stream_traffic,
            "memory": self.api.async_stream_memory,
            "connections": self.api.async_stream_connections,
        }
        for key, capability in STREAM_ENDPOINT_CAPABILITIES.items():
            if not capabilities.get(capability):
                continue
            self._stream_tasks[key] = (
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_stream_endpoint(key, factories[key]),
                    f"{DOMAIN} {key} stream",
                )
            )

    async def async_stop_streams(self) -> None:
        """Stop all background subscriptions."""
        self._cancel_stream_publish()
        await self.async_stop_log_stream()
        tasks = list(self._stream_tasks.values())
        self._stream_tasks.clear()
        self._stream_last_frame.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _async_stream_endpoint(
        self,
        key: str,
        factory: Callable[[], AsyncIterator[dict[str, Any]]],
    ) -> None:
        """Consume one endpoint and reconnect with backoff."""
        delay = STREAM_RECONNECT_INITIAL
        while True:
            try:
                async for payload in factory():
                    delay = STREAM_RECONNECT_INITIAL
                    self._record_stream_frame(key, payload)
            except asyncio.CancelledError:
                raise
            except APIAuthError:
                _LOGGER.debug(
                    "%s stream authentication failed for entry %s",
                    key,
                    self.config_entry.entry_id,
                )
                return
            except ClashAPIError as err:
                _LOGGER.debug(
                    "%s stream disconnected for entry %s: %s",
                    key,
                    self.config_entry.entry_id,
                    err,
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected %s stream failure for entry %s",
                    key,
                    self.config_entry.entry_id,
                )
            self._stream_last_frame.pop(key, None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, STREAM_RECONNECT_MAX)

    def _record_stream_frame(self, key: str, payload: dict[str, Any]) -> None:
        """Merge a pushed frame into the snapshot used by entities."""
        if not payload:
            return
        if key == "memory":
            inuse = payload.get("inuse")
            if (
                isinstance(inuse, bool)
                or not isinstance(inuse, (int, float))
                or inuse <= 0
            ):
                return
        self._latest[key] = payload
        self._stream_last_frame[key] = time.monotonic()
        self._queue_stream_publish()

    def _queue_stream_publish(self) -> None:
        """Coalesce pushed frames into one entity update per interval."""
        if self._stream_publish_unsub is not None:
            return
        self._stream_publish_unsub = async_call_later(
            self.hass,
            STREAM_PUBLISH_INTERVAL,
            self._async_publish_stream_data,
        )

    async def _async_publish_stream_data(self, _now: Any) -> None:
        """Push the merged snapshot without resetting the poll schedule."""
        self._stream_publish_unsub = None
        if not self._latest:
            return
        self.data = self._build_entity_data(self._latest)
        self.async_update_listeners()

    def _cancel_stream_publish(self) -> None:
        """Cancel a pending coalesced publish."""
        if self._stream_publish_unsub is not None:
            self._stream_publish_unsub()
            self._stream_publish_unsub = None

    def async_start_log_stream(self) -> None:
        """Start the background log subscription."""
        if self._log_stream_task is not None:
            return
        self._log_stream_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_stream_logs(),
            f"{DOMAIN} log stream",
        )

    async def async_stop_log_stream(self) -> None:
        """Stop the background log subscription."""
        task = self._log_stream_task
        self._log_stream_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _async_stream_logs(self) -> None:
        """Consume core logs and reconnect with backoff."""
        delay = LOG_RECONNECT_INITIAL
        while True:
            try:
                async for entry in self.api.async_stream_logs(level="warning"):
                    delay = LOG_RECONNECT_INITIAL
                    self._record_log(entry.level)
            except asyncio.CancelledError:
                raise
            except APIAuthError:
                _LOGGER.debug(
                    "Log stream authentication failed for entry %s",
                    self.config_entry.entry_id,
                )
                return
            except ClashAPIError as err:
                _LOGGER.debug(
                    "Log stream disconnected for entry %s: %s",
                    self.config_entry.entry_id,
                    err,
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected log stream failure for entry %s",
                    self.config_entry.entry_id,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, LOG_RECONNECT_MAX)

    def _record_log(self, level: str) -> None:
        """Record one log event inside the sliding window."""
        normalized = level.lower()
        if normalized == "warn":
            normalized = "warning"
        if normalized not in self._log_events:
            return
        now = dt_util.utcnow().timestamp()
        self._log_events[normalized].append(now)
        self._prune_log_events(now)

    def _prune_log_events(self, now: float | None = None) -> None:
        """Drop log events older than the configured window."""
        if now is None:
            now = dt_util.utcnow().timestamp()
        boundary = now - self.log_window
        for events in self._log_events.values():
            while events and events[0] <= boundary:
                events.popleft()

    def _build_log_entities(self) -> list[ClashEntityData]:
        """Create sliding-window log counters."""
        self._prune_log_events()
        return [
            ClashEntityData(
                name=None,
                state=len(self._log_events[level]),
                entity_type="log_count_sensor",
                translation_key=f"{level}_log_count",
                entity_category=EntityCategory.DIAGNOSTIC,
                attributes={"window_seconds": self.log_window},
                unique_key=f"{level}_log_count",
            )
            for level in LOG_LEVELS
        ]

    @property
    def log_counts(self) -> dict[str, int]:
        """Return the current per-level log counts."""
        self._prune_log_events()
        return {level: len(events) for level, events in self._log_events.items()}

    def _build_entity_data(self, response: dict[str, Any]) -> list[ClashEntityData]:
        """Construct entity descriptions from API response."""
        capabilities = self.api.capabilities or {}
        entity_data: list[ClashEntityData] = []

        if capabilities.get("traffic"):
            entity_data.extend(
                self._build_traffic_entities(response.get("traffic", {}))
            )
        if capabilities.get("connections"):
            entity_data.extend(
                self._build_connection_entities(response.get("connections", {}))
            )
        if capabilities.get("memory"):
            entity_data.extend(self._build_memory_entities(response.get("memory", {})))
        if capabilities.get("ws_logs"):
            entity_data.extend(self._build_log_entities())
        if capabilities.get("providers_proxies") or capabilities.get("providers_rules"):
            entity_data.extend(
                self._build_provider_entities(
                    response.get("providers_proxies")
                    if capabilities.get("providers_proxies")
                    else None,
                    response.get("providers_rules")
                    if capabilities.get("providers_rules")
                    else None,
                )
            )
        if capabilities.get("cache_fakeip_flush"):
            entity_data.append(self._build_fakeip_button())
        if capabilities.get("cache_dns_flush"):
            entity_data.append(self._build_dns_flush_button())

        for item in entity_data:
            id_source = (
                item.unique_key or item.name or item.translation_key or item.entity_type
            )
            item.unique_id = (
                f"{self.device_id}"
                f"_{item.entity_type}"
                f"_{id_source.lower().replace(' ', '_')}"
            )

        self._data_by_unique_id = {item.unique_id: item for item in entity_data}
        return entity_data

    @staticmethod
    def _build_traffic_entities(traffic: dict[str, Any]) -> list[ClashEntityData]:
        """Create traffic related entities."""
        if not traffic:
            return []

        return [
            ClashEntityData(
                name=None,
                state=traffic.get("up"),
                entity_type="traffic_sensor",
                translation_key="up_speed",
                unique_key="upload_speed",
            ),
            ClashEntityData(
                name=None,
                state=traffic.get("down"),
                entity_type="traffic_sensor",
                translation_key="down_speed",
                unique_key="download_speed",
            ),
        ]

    @staticmethod
    def _build_connection_entities(
        connections: dict[str, Any],
    ) -> list[ClashEntityData]:
        """Create connection related entities."""
        if not connections:
            return []

        return [
            ClashEntityData(
                name=None,
                state=connections.get("uploadTotal"),
                entity_type="total_traffic_sensor",
                translation_key="up_traffic",
                unique_key="upload_traffic",
            ),
            ClashEntityData(
                name=None,
                state=connections.get("downloadTotal"),
                entity_type="total_traffic_sensor",
                translation_key="down_traffic",
                unique_key="download_traffic",
            ),
            ClashEntityData(
                name=None,
                state=len(connections.get("connections", []) or []),
                entity_type="connection_sensor",
                translation_key="connection_number",
                unique_key="connection_number",
            ),
        ]

    @staticmethod
    def _build_memory_entities(memory: dict[str, Any]) -> list[ClashEntityData]:
        """Create memory related entities."""
        if not memory:
            return []

        return [
            ClashEntityData(
                name=None,
                state=memory.get("inuse"),
                entity_type="memory_sensor",
                translation_key="memory_used",
                unique_key="memory_used",
            )
        ]

    def _build_fakeip_button(self) -> ClashEntityData:
        """Create FakeIP cache flush button entity."""
        return ClashEntityData(
            name=None,
            entity_type="fakeip_flush_button",
            icon="mdi:cached",
            translation_key="flush_cache",
            entity_category=EntityCategory.DIAGNOSTIC,
            action={
                "method": self.api.async_request,
                "args": ("POST", "cache/fakeip/flush"),
                "kwargs": {},
            },
            unique_key="flush_fakeip_cache",
        )

    def _build_dns_flush_button(self) -> ClashEntityData:
        """Create DNS cache flush button entity."""
        return ClashEntityData(
            name=None,
            entity_type="dns_flush_button",
            icon="mdi:cached",
            translation_key="flush_dns_cache",
            entity_category=EntityCategory.DIAGNOSTIC,
            action={
                "method": self.api.async_request,
                "args": ("POST", "cache/dns/flush"),
                "kwargs": {},
            },
            unique_key="flush_dns_cache",
        )

    def _build_provider_entities(
        self,
        providers_proxies: dict[str, Any] | None,
        providers_rules: dict[str, Any] | None,
    ) -> list[ClashEntityData]:
        """Create entities for provider metrics."""

        proxy_provider_map = (
            providers_proxies.get("providers")
            if isinstance(providers_proxies, dict)
            else None
        )
        rule_provider_map = (
            providers_rules.get("providers")
            if isinstance(providers_rules, dict)
            else None
        )

        if not isinstance(proxy_provider_map, dict):
            proxy_provider_map = None
        if not isinstance(rule_provider_map, dict):
            rule_provider_map = None

        entity_data: list[ClashEntityData] = []
        for provider_type, providers in (
            ("proxy", proxy_provider_map),
            ("rule", rule_provider_map),
        ):
            if providers is not None:
                entity_data.append(
                    ClashEntityData(
                        name=None,
                        state=len(providers),
                        entity_type="provider_count_sensor",
                        translation_key=f"{provider_type}_provider_count",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        unique_key=f"{provider_type}_provider_count",
                    )
                )

        return entity_data

    def get_data_by_unique_id(self, unique_id: str) -> ClashEntityData | None:
        """Retrieve data by unique ID."""
        return self._data_by_unique_id.get(unique_id)
