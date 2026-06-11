from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url="https://accounts.spotify.com/authorize",
        token_url="https://accounts.spotify.com/api/token",
    )
