"""Initializations for Clash Controller."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_API_URL, DOMAIN
from .coordinator import ClashControllerCoordinator
from .services import ClashServicesSetup

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
]


@dataclass(slots=True)
class RuntimeData:
    """Class to hold integration data."""

    coordinator: ClashControllerCoordinator


type ClashControllerConfigEntry = ConfigEntry[RuntimeData]


def _normalize_legacy_url(api_url: str, use_ssl: bool) -> str:
    """Return a full base URL for entries created before minor version 3."""
    api_url = api_url.strip()
    if not api_url.lower().startswith(("http://", "https://")):
        api_url = f"{'https' if use_ssl else 'http'}://{api_url}"
    if not api_url.endswith("/"):
        api_url += "/"
    return api_url


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide service actions."""
    ClashServicesSetup(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ClashControllerConfigEntry
) -> bool:
    """Set up Clash Controller from a config entry."""

    coordinator = ClashControllerCoordinator(hass, config_entry)
    await coordinator.async_config_entry_first_refresh()

    config_entry.runtime_data = RuntimeData(coordinator)
    coordinator.async_start_streams()
    config_entry.async_on_unload(coordinator.async_stop_streams)
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: ClashControllerConfigEntry
) -> bool:
    """Remove obsolete runtime state from persisted entry data."""
    if config_entry.version == 1 and config_entry.minor_version < 2:
        data = dict(config_entry.data)
        data.pop("available_endpoints", None)
        data.pop("capabilities", None)
        hass.config_entries.async_update_entry(
            config_entry,
            data=data,
            minor_version=2,
        )
    if config_entry.version == 1 and config_entry.minor_version < 3:
        data = dict(config_entry.data)
        use_ssl = data.pop("use_ssl", False)
        api_url = data.get(CONF_API_URL)
        if isinstance(api_url, str):
            data[CONF_API_URL] = _normalize_legacy_url(api_url, use_ssl)
        hass.config_entries.async_update_entry(
            config_entry,
            data=data,
            minor_version=3,
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: ClashControllerConfigEntry
) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )
    return unload_ok
