from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_DISCOVERY_TIMEOUT,
    DOMAIN,
    NAME,
    SERVICE_ACTIVATE_ALL,
    SERVICE_REFRESH_NATIVE_SPOTIFY,
    SERVICE_RUN_AUTO_CYCLE,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            SpotifyConnectActionButton(entry, "run_auto_cycle", "Run auto cycle", SERVICE_RUN_AUTO_CYCLE, {}),
            SpotifyConnectActionButton(
                entry,
                "activate_all",
                "Activate all discovered sources",
                SERVICE_ACTIVATE_ALL,
                {"timeout": DEFAULT_DISCOVERY_TIMEOUT},
            ),
            SpotifyConnectActionButton(
                entry,
                "refresh_native_spotify",
                "Refresh native Spotify sources",
                SERVICE_REFRESH_NATIVE_SPOTIFY,
                {},
            ),
        ]
    )


class SpotifyConnectActionButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        name: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        self._entry = entry
        self._service = service
        self._service_data = service_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title or NAME,
            "manufacturer": "Spotify",
            "model": "Spotify Connect Source Activator",
            "sw_version": VERSION,
        }

    async def async_press(self) -> None:
        data = {**self._service_data, "config_entry_id": self._entry.entry_id}
        _LOGGER.debug("Button pressed: service=%s data=%s", self._service, data)
        await self.hass.services.async_call(DOMAIN, self._service, data, blocking=True)
