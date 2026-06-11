from __future__ import annotations

from typing import Any
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ID, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import selector
from homeassistant.components import zeroconf

from spotifywebapipython import SpotifyClient

from .const import (
    DOMAIN,
    NAME,
    SPOTIFY_SCOPES,
    CONF_RELOAD_NATIVE_SPOTIFY,
    CONF_AUTO_DISCOVERY_ENABLED,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_DISCOVERY_TIMEOUT,
    CONF_VERIFY_TIMEOUT,
    DEFAULT_RELOAD_NATIVE_SPOTIFY,
    DEFAULT_AUTO_DISCOVERY_ENABLED,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_DISCOVERY_TIMEOUT,
    DEFAULT_VERIFY_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class SpotifyConnectSourceActivatorConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """OAuth2 config flow for Spotify Connect Source Activator."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(SPOTIFY_SCOPES), "show_dialog": "true"}

    async def async_oauth_create_entry(self, data: dict[str, Any]):
        """Create entry after Spotify OAuth completes."""
        zc = await zeroconf.async_get_instance(self.hass)
        token_storage_dir = f"{self.hass.config.config_dir}/.storage"
        token_storage_file = f"{DOMAIN}_tokens.json"

        client_id = getattr(self.flow_impl, "client_id", None)

        def _build_and_profile() -> tuple[str, str]:
            client = SpotifyClient(
                None,
                token_storage_dir,
                token_storage_file,
                None,
                zc,
                None,
                None,
                None,
                0,
                False,
                None,
                None,
            )
            try:
                client.SetAuthTokenFromToken(client_id, data["token"], None)
                return client.UserProfile.Id, client.UserProfile.DisplayName or client.UserProfile.Id
            finally:
                try:
                    client.Dispose()
                except Exception:  # noqa: BLE001
                    pass

        try:
            user_id, display_name = await self.hass.async_add_executor_job(_build_and_profile)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to validate Spotify OAuth token")
            return self.async_abort(reason="cannot_connect", description_placeholders={"error": str(err)})

        await self.async_set_unique_id(f"{user_id}_{DOMAIN}")
        self._abort_if_unique_id_configured()

        data[CONF_ID] = user_id
        data[CONF_NAME] = display_name
        return self.async_create_entry(title=f"{NAME}: {display_name}", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return SpotifyConnectSourceActivatorOptionsFlow()


class SpotifyConnectSourceActivatorOptionsFlow(config_entries.OptionsFlow):
    """Options flow. Uses HA's native OptionsFlow config_entry property."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AUTO_DISCOVERY_ENABLED,
                    default=options.get(CONF_AUTO_DISCOVERY_ENABLED, DEFAULT_AUTO_DISCOVERY_ENABLED),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_SCAN_INTERVAL_MINUTES,
                    default=options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=1440, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_DISCOVERY_TIMEOUT,
                    default=options.get(CONF_DISCOVERY_TIMEOUT, DEFAULT_DISCOVERY_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_VERIFY_TIMEOUT,
                    default=options.get(CONF_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_RELOAD_NATIVE_SPOTIFY,
                    default=options.get(CONF_RELOAD_NATIVE_SPOTIFY, DEFAULT_RELOAD_NATIVE_SPOTIFY),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
