from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import asyncio
import functools
import json
import logging
import os
import threading
from typing import Any, Callable

import requests

import voluptuous as vol

from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session, async_get_config_entry_implementation
try:
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover, older HA fallback
    SupportsResponse = None  # type: ignore[assignment]

from spotifywebapipython import SpotifyClient, SpotifyDiscovery, SpotifyApiError, SpotifyWebApiError
from spotifywebapipython.zeroconfapi import ZeroconfConnect
try:
    from spotifywebapipython.zeroconfapi import SpotifyZeroconfApiError
except Exception:  # noqa: BLE001
    SpotifyZeroconfApiError = Exception  # type: ignore[misc,assignment]

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CPATH,
    ATTR_DELAY,
    ATTR_HOST,
    ATTR_PORT,
    ATTR_RELOAD_DELAY,
    ATTR_RELOAD_NATIVE_SPOTIFY,
    ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS,
    ATTR_SPOTIFY_CONFIG_ENTRY_ID,
    ATTR_TIMEOUT,
    ATTR_USE_SSL,
    ATTR_VERIFY_AFTER,
    ATTR_VERIFY_BEFORE,
    ATTR_VERIFY_TIMEOUT,
    ATTR_VERSION,
    CONF_RELOAD_NATIVE_SPOTIFY,
    CONF_AUTO_DISCOVERY_ENABLED,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_DISCOVERY_TIMEOUT,
    CONF_VERIFY_TIMEOUT,
    DEFAULT_AUTO_DISCOVERY_ENABLED,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_DISCOVERY_TIMEOUT,
    DEFAULT_CPATH,
    DEFAULT_DELAY,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_TIMEOUT,
    DEFAULT_RELOAD_DELAY,
    DEFAULT_RELOAD_NATIVE_SPOTIFY,
    DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS,
    DEFAULT_ZC_VERSION,
    DOMAIN,
    NATIVE_SPOTIFY_DOMAIN,
    SERVICE_ACTIVATE_ALL,
    SERVICE_ACTIVATE_DEVICE,
    SERVICE_DISCONNECT_ALL,
    SERVICE_DISCONNECT_DEVICE,
    SERVICE_DISCOVER,
    SERVICE_GET_INFO,
    SERVICE_REFRESH_NATIVE_SPOTIFY,
    SERVICE_RUN_AUTO_CYCLE,
    SERVICE_VERIFY_DEVICE,
    SPOTIFY_SCOPES,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["button"]
TOKEN_LOCK = threading.Lock()


@dataclass(slots=True)
class RuntimeData:
    entry: ConfigEntry
    session: OAuth2Session
    implementation: Any
    client: SpotifyClient
    token_storage_dir: str
    token_storage_file: str
    auto_lock: asyncio.Lock
    operation_lock: asyncio.Lock
    auto_unsub: Callable[[], None] | None = None
    last_auto_result: dict[str, Any] | None = None
    auto_suspended_until: float = 0.0
    last_token_key: str | None = None


def _mask(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def _to_dict(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_dict(v) for v in value]
    to_dictionary = getattr(value, "ToDictionary", None)
    if callable(to_dictionary):
        return to_dictionary()
    return str(value)


def _service_response_supported() -> Any:
    if SupportsResponse is None:
        return None
    return SupportsResponse.OPTIONAL


def _get_runtime(hass: HomeAssistant, config_entry_id: str | None = None) -> RuntimeData:
    entries: dict[str, RuntimeData] = hass.data.get(DOMAIN, {})
    if not entries:
        raise ServiceValidationError("No Spotify Connect Source Activator config entry is loaded")
    if config_entry_id:
        runtime = entries.get(config_entry_id)
        if runtime is None:
            raise ServiceValidationError(f"Config entry not found or not loaded: {config_entry_id}")
        return runtime
    if len(entries) > 1:
        raise ServiceValidationError("Multiple Spotify accounts are configured; pass config_entry_id")
    return next(iter(entries.values()))



async def _refresh_native_spotify_entries(
    hass: HomeAssistant,
    *,
    spotify_config_entry_id: str | None = None,
    delay: float = 0,
) -> dict[str, Any]:
    """Refresh native Spotify coordinators without unloading/reloading the config entry."""
    if delay > 0:
        _LOGGER.debug("Waiting %.1fs before refreshing native Spotify device coordinator", delay)
        await asyncio.sleep(delay)

    if spotify_config_entry_id:
        entry = hass.config_entries.async_get_entry(spotify_config_entry_id)
        if entry is None:
            raise ServiceValidationError(f"Native Spotify config entry not found: {spotify_config_entry_id}")
        if entry.domain != NATIVE_SPOTIFY_DOMAIN:
            raise ServiceValidationError(
                f"Config entry {spotify_config_entry_id} is domain {entry.domain}, not {NATIVE_SPOTIFY_DOMAIN}"
            )
        entries = [entry]
    else:
        entries = list(hass.config_entries.async_entries(NATIVE_SPOTIFY_DOMAIN))

    if not entries:
        return {"refreshed": False, "count": 0, "entries": [], "reason": "no_native_spotify_entries"}

    results: list[dict[str, Any]] = []
    for entry in entries:
        runtime_data = getattr(entry, "runtime_data", None)
        devices = getattr(runtime_data, "devices", None) if runtime_data is not None else None
        coordinator = getattr(runtime_data, "coordinator", None) if runtime_data is not None else None
        if devices is None or not hasattr(devices, "async_request_refresh"):
            result: dict[str, Any] = {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "ok": False,
                "method": "coordinator_refresh",
                "reason": "native_spotify_runtime_or_device_coordinator_not_available",
            }
            results.append(result)
            continue

        before_devices = getattr(devices, "data", None) or []
        before_names = [getattr(item, "name", None) for item in before_devices]
        _LOGGER.debug(
            "Refreshing native Spotify devices: title=%s entry_id=%s before=%s",
            entry.title,
            entry.entry_id,
            before_names,
        )
        try:
            await devices.async_request_refresh()
            # Playback refresh is not needed for source_list itself, but it keeps current source/state coherent.
            if coordinator is not None and hasattr(coordinator, "async_request_refresh"):
                await coordinator.async_request_refresh()
            after_devices = getattr(devices, "data", None) or []
            after = [
                {
                    "name": getattr(item, "name", None),
                    "id": getattr(item, "device_id", None),
                    "type": getattr(item, "device_type", None) or getattr(item, "type", None),
                    "is_active": getattr(item, "is_active", None),
                    "is_restricted": getattr(item, "is_restricted", None),
                }
                for item in after_devices
            ]
            results.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "ok": True,
                    "method": "coordinator_refresh",
                    "before_count": len(before_devices),
                    "after_count": len(after_devices),
                    "before_names": before_names,
                    "after_devices": after,
                }
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to refresh native Spotify coordinators for config entry %s", entry.entry_id)
            result = {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "ok": False,
                "method": "coordinator_refresh",
                "error": str(err),
            }
            results.append(result)
    return {"refreshed": any(item.get("ok") for item in results), "count": len(results), "entries": results}


async def _maybe_refresh_native_spotify(hass: HomeAssistant, runtime: RuntimeData, data: dict[str, Any]) -> dict[str, Any] | None:
    refresh_requested = data.get(ATTR_RELOAD_NATIVE_SPOTIFY)
    if refresh_requested is None:
        refresh_requested = runtime.entry.options.get(CONF_RELOAD_NATIVE_SPOTIFY, DEFAULT_RELOAD_NATIVE_SPOTIFY)
    if not bool(refresh_requested):
        return None
    return await _refresh_native_spotify_entries(
        hass,
        spotify_config_entry_id=data.get(ATTR_SPOTIFY_CONFIG_ENTRY_ID),
        delay=float(data.get(ATTR_RELOAD_DELAY, DEFAULT_RELOAD_DELAY)),
    )




def _now_monotonic() -> float:
    import time

    return time.monotonic()


def _suspend_auto_discovery(runtime: RuntimeData, seconds: int | float) -> dict[str, Any]:
    seconds = max(0.0, float(seconds or 0))
    if seconds <= 0:
        return {"suspended": False, "seconds": 0}
    runtime.auto_suspended_until = max(runtime.auto_suspended_until, _now_monotonic() + seconds)
    return {"suspended": True, "seconds": seconds, "until_monotonic": runtime.auto_suspended_until}


def _option_bool(entry: ConfigEntry, key: str, default: bool) -> bool:
    return bool(entry.options.get(key, default))


def _option_int(entry: ConfigEntry, key: str, default: int) -> int:
    try:
        return int(entry.options.get(key, default))
    except (TypeError, ValueError):
        return default


async def _start_or_stop_auto_discovery(hass: HomeAssistant, runtime: RuntimeData) -> None:
    """Start/stop the periodic discovery+activation scheduler for one config entry."""
    if runtime.auto_unsub is not None:
        runtime.auto_unsub()
        runtime.auto_unsub = None

    enabled = _option_bool(runtime.entry, CONF_AUTO_DISCOVERY_ENABLED, DEFAULT_AUTO_DISCOVERY_ENABLED)
    interval_minutes = max(1, _option_int(runtime.entry, CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES))
    if not enabled:
        _LOGGER.debug("Auto discovery disabled for entry_id=%s", runtime.entry.entry_id)
        return

    _LOGGER.debug(
        "Auto discovery enabled for entry_id=%s interval=%smin timeout=%ss verify_timeout=%ss",
        runtime.entry.entry_id,
        interval_minutes,
        _option_int(runtime.entry, CONF_DISCOVERY_TIMEOUT, DEFAULT_DISCOVERY_TIMEOUT),
        _option_int(runtime.entry, CONF_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT),
    )

    async def _tick(_now) -> None:
        try:
            await _async_run_auto_cycle(hass, runtime, reason="scheduled")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Scheduled Spotify Connect source activation cycle failed")

    runtime.auto_unsub = async_track_time_interval(hass, _tick, timedelta(minutes=interval_minutes))
    runtime.entry.async_create_background_task(
        hass,
        _async_run_auto_cycle(hass, runtime, reason="options_enabled"),
        f"{DOMAIN}_initial_auto_cycle",
    )


async def _refresh_client_token(hass: HomeAssistant, runtime: RuntimeData) -> None:
    """Refresh the HA OAuth token and only re-seed SpotifyClient when the token changed.

    SetAuthTokenFromToken is not a cheap no-op in spotifywebapipython: it can restart
    the internal Spotify Connect Directory task. Doing that before every local ZeroConf
    operation races some devices, especially LG soundbars whose advertised port can
    briefly refuse connections while the directory task is restarting.
    """
    await runtime.session.async_ensure_token_valid()
    token = runtime.session.token or {}
    token_key = f"{token.get('access_token')}:{token.get('expires_at')}"
    if token_key == runtime.last_token_key:
        return

    client_id = getattr(runtime.implementation, "client_id", None)
    await hass.async_add_executor_job(
        runtime.client.SetAuthTokenFromToken,
        client_id,
        token,
        None,
    )
    runtime.last_token_key = token_key


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level service actions.

    Services are intentionally registered before any config entry is loaded so
    automations can be validated and calls fail with a clear message when no
    Spotify account entry is currently available.
    """
    _register_services_once(hass)
    return True



async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    implementation = await async_get_config_entry_implementation(hass, entry)
    session = OAuth2Session(hass, entry, implementation)
    await session.async_ensure_token_valid()
    token_scopes = set(str((session.token or {}).get("scope", "")).split())
    required_scopes = set(SPOTIFY_SCOPES)
    if not token_scopes.issuperset(required_scopes):
        missing = sorted(required_scopes - token_scopes)
        _LOGGER.warning("Spotify OAuth token is missing required scope(s): %s", ", ".join(missing))
        raise ConfigEntryAuthFailed("Spotify OAuth token scopes changed; reauthorize this integration")
    zc = await zeroconf.async_get_instance(hass)

    token_storage_dir = os.path.join(hass.config.config_dir, ".storage")
    token_storage_file = f"{DOMAIN}_{entry.data.get(CONF_ID, entry.entry_id)}_tokens.json"
    def _token_updater() -> dict[str, Any]:
        # SpotifyClient may call this from an executor thread. Use HA's OAuth implementation,
        # persist the refreshed token back to the config entry, then return it to the library.
        with TOKEN_LOCK:
            token = asyncio.run_coroutine_threadsafe(
                session.implementation.async_refresh_token(session.config_entry.data["token"]),
                hass.loop,
            ).result()
            session.hass.add_job(
                functools.partial(
                    session.hass.config_entries.async_update_entry,
                    session.config_entry,
                    data={**session.config_entry.data, "token": token},
                )
            )
            return token

    def _make_client() -> SpotifyClient:
        # Connect-only integration.  Keep the Spotify Connect Directory enabled,
        # but do not depend on Spotify Connect token files or non-Connect paths.
        # We pass the canonical login id as username for spotifywebapipython flows
        # that require a string value; no Spotify account password is requested.
        connect_login_id = str(entry.data.get(CONF_ID) or "")
        connect_username = connect_login_id
        connect_password = ""
        client = SpotifyClient(
            None,
            token_storage_dir,
            token_storage_file,
            _token_updater,
            zc,
            connect_username,
            connect_password,
            connect_login_id,
            2.0,
            True,
            None,
            None,
        )
        client.SetAuthTokenFromToken(getattr(implementation, "client_id", None), session.token, None)
        return client

    client = await hass.async_add_executor_job(_make_client)
    runtime = RuntimeData(
        entry,
        session,
        implementation,
        client,
        token_storage_dir,
        token_storage_file,
        asyncio.Lock(),
        asyncio.Lock(),
    )
    token = session.token or {}
    runtime.last_token_key = f"{token.get('access_token')}:{token.get('expires_at')}"
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    _register_services_once(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await _start_or_stop_auto_discovery(hass, runtime)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "Loaded %s for Spotify account %s (%s)",
        DOMAIN,
        entry.data.get(CONF_NAME),
        entry.data.get(CONF_ID),
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        await _start_or_stop_auto_discovery(hass, runtime)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        if runtime.auto_unsub is not None:
            runtime.auto_unsub()
            runtime.auto_unsub = None
        await hass.async_add_executor_job(runtime.client.Dispose)
    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)
    return True


def _register_services_once(hass: HomeAssistant) -> None:
    marker = f"{DOMAIN}_services_registered"
    if hass.data.get(marker):
        return
    hass.data[marker] = True

    def register(name: str, schema: vol.Schema, handler: Callable[[ServiceCall], Any]) -> None:
        kwargs: dict[str, Any] = {"schema": schema}
        supports = _service_response_supported()
        if supports is not None:
            kwargs["supports_response"] = supports
        hass.services.async_register(DOMAIN, name, handler, **kwargs)

    common = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
    register(
        SERVICE_DISCOVER,
        vol.Schema({**common, vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(int)}),
        functools.partial(_handle_discover, hass),
    )
    host_schema = {
        **common,
        vol.Required(ATTR_HOST): cv.string,
        vol.Optional(ATTR_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(ATTR_CPATH, default=DEFAULT_CPATH): cv.string,
        vol.Optional(ATTR_VERSION, default=DEFAULT_ZC_VERSION): cv.string,
        vol.Optional(ATTR_USE_SSL, default=False): cv.boolean,
    }
    register(SERVICE_GET_INFO, vol.Schema(host_schema), functools.partial(_handle_get_info, hass))
    register(
        SERVICE_VERIFY_DEVICE,
        vol.Schema(host_schema),
        functools.partial(_handle_verify_device, hass),
    )
    register(
        SERVICE_REFRESH_NATIVE_SPOTIFY,
        vol.Schema(
            {
                **common,
                vol.Optional(ATTR_SPOTIFY_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(ATTR_RELOAD_DELAY, default=0): vol.Coerce(float),
            }
        ),
        functools.partial(_handle_refresh_native_spotify, hass),
    )
    register(
        SERVICE_RUN_AUTO_CYCLE,
        vol.Schema(common),
        functools.partial(_handle_run_auto_cycle, hass),
    )
    register(
        SERVICE_ACTIVATE_DEVICE,
        vol.Schema(
            {
                **host_schema,
                vol.Optional(ATTR_VERIFY_BEFORE, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_AFTER, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_TIMEOUT, default=DEFAULT_VERIFY_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_DELAY, default=DEFAULT_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_RELOAD_NATIVE_SPOTIFY): cv.boolean,
                vol.Optional(ATTR_SPOTIFY_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(ATTR_RELOAD_DELAY, default=DEFAULT_RELOAD_DELAY): vol.Coerce(float),
            }
        ),
        functools.partial(_handle_activate_device, hass),
    )
    register(
        SERVICE_DISCONNECT_DEVICE,
        vol.Schema(
            {
                **host_schema,
                vol.Optional(ATTR_VERIFY_BEFORE, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_AFTER, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_TIMEOUT, default=DEFAULT_VERIFY_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_DELAY, default=DEFAULT_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_RELOAD_NATIVE_SPOTIFY): cv.boolean,
                vol.Optional(ATTR_SPOTIFY_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(ATTR_RELOAD_DELAY, default=DEFAULT_RELOAD_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS, default=DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS): vol.Coerce(int),
            }
        ),
        functools.partial(_handle_disconnect_device, hass),
    )

    register(
        SERVICE_ACTIVATE_ALL,
        vol.Schema(
            {
                **common,
                vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_VERIFY_BEFORE, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_AFTER, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_TIMEOUT, default=DEFAULT_VERIFY_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_DELAY, default=DEFAULT_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_RELOAD_NATIVE_SPOTIFY): cv.boolean,
                vol.Optional(ATTR_SPOTIFY_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(ATTR_RELOAD_DELAY, default=DEFAULT_RELOAD_DELAY): vol.Coerce(float),
            }
        ),
        functools.partial(_handle_activate_all, hass),
    )
    register(
        SERVICE_DISCONNECT_ALL,
        vol.Schema(
            {
                **common,
                vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_VERIFY_BEFORE, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_AFTER, default=True): cv.boolean,
                vol.Optional(ATTR_VERIFY_TIMEOUT, default=DEFAULT_VERIFY_TIMEOUT): vol.Coerce(int),
                vol.Optional(ATTR_DELAY, default=DEFAULT_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_RELOAD_NATIVE_SPOTIFY): cv.boolean,
                vol.Optional(ATTR_SPOTIFY_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(ATTR_RELOAD_DELAY, default=DEFAULT_RELOAD_DELAY): vol.Coerce(float),
                vol.Optional(ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS, default=DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS): vol.Coerce(int),
            }
        ),
        functools.partial(_handle_disconnect_all, hass),
    )




def _discover_sync(runtime: RuntimeData, timeout: int) -> list[dict[str, Any]]:
    discovery = SpotifyDiscovery(runtime.client.ZeroconfClient, printToConsole=False)
    discovery.DiscoverDevices(timeout)
    return [_to_dict(item) for item in discovery.DiscoveryResults]


async def _handle_discover(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    timeout = int(call.data[ATTR_TIMEOUT])
    async with runtime.operation_lock:
        await _refresh_client_token(hass, runtime)

        _LOGGER.debug("Discovering Spotify Connect devices: timeout=%s", timeout)
        devices = await hass.async_add_executor_job(_discover_sync, runtime, timeout)
    _LOGGER.debug("Discovered %d Spotify Connect device(s)", len(devices))
    return {"devices": devices, "count": len(devices)}


def _make_zconn(runtime: RuntimeData, data: dict[str, Any]) -> ZeroconfConnect:
    return ZeroconfConnect(
        hostIpAddress=data[ATTR_HOST],
        hostIpPort=int(data.get(ATTR_PORT, DEFAULT_PORT)),
        cpath=data.get(ATTR_CPATH, DEFAULT_CPATH),
        version=data.get(ATTR_VERSION, DEFAULT_ZC_VERSION),
        useSSL=bool(data.get(ATTR_USE_SSL, False)),
        tokenStorageDir=runtime.token_storage_dir,
        tokenStorageFile=runtime.token_storage_file,
        tokenAuthInBrowser=False,
    )


async def _handle_get_info(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))

    def _get() -> dict[str, Any]:
        return _to_dict(_make_zconn(runtime, call.data).GetInformation())

    _LOGGER.debug("ZeroConf getInfo: host=%s port=%s cpath=%s ssl=%s", call.data[ATTR_HOST], call.data.get(ATTR_PORT), call.data.get(ATTR_CPATH), call.data.get(ATTR_USE_SSL))
    async with runtime.operation_lock:
        info = await hass.async_add_executor_job(_get)
    return {"info": info}


async def _handle_verify_device(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    async with runtime.operation_lock:
        await _refresh_client_token(hass, runtime)
        return await hass.async_add_executor_job(_verify_device_sync, runtime, call.data)



async def _handle_refresh_native_spotify(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    return await _refresh_native_spotify_entries(
        hass,
        spotify_config_entry_id=call.data.get(ATTR_SPOTIFY_CONFIG_ENTRY_ID),
        delay=float(call.data.get(ATTR_RELOAD_DELAY, 0)),
    )


def _spotify_api_error_reason(err: Exception) -> str:
    message = str(getattr(err, "Message", err))
    if "Spotify Desktop Player authorization token was not found" in message:
        return "missing_spotify_desktop_token"
    if "SpotifyConnectUsername" in message:
        return "missing_spotify_connect_username"
    if "SpotifyConnectPassword" in message:
        return "missing_spotify_connect_password"
    return "spotify_activation_error"

def _verify_device_sync(runtime: RuntimeData, data: dict[str, Any]) -> dict[str, Any]:
    zconn = _make_zconn(runtime, data)
    info = zconn.GetInformation()
    info_dict = _to_dict(info)
    device_id = getattr(info, "DeviceId", None) or info_dict.get("DeviceId") or info_dict.get("deviceId")
    player_device = runtime.client.GetPlayerDevice(device_id, True) if device_id else None
    return {
        "info": info_dict,
        "device_id": device_id,
        "available_in_spotify": player_device is not None,
        "player_device": _to_dict(player_device),
    }


async def _handle_activate_device(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Activate one local Spotify Connect ZeroConf endpoint directly."""
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    _LOGGER.debug(
        "Activating Spotify Connect endpoint host=%s port=%s cpath=%s ssl=%s verify_before=%s verify_after=%s",
        call.data[ATTR_HOST],
        call.data.get(ATTR_PORT),
        call.data.get(ATTR_CPATH),
        call.data.get(ATTR_USE_SSL),
        call.data.get(ATTR_VERIFY_BEFORE),
        call.data.get(ATTR_VERIFY_AFTER),
    )
    try:
        async with runtime.operation_lock:
            await _refresh_client_token(hass, runtime)
            result = await hass.async_add_executor_job(_activate_device_sync, runtime, dict(call.data))
        result["native_spotify_refresh"] = (
            await _maybe_refresh_native_spotify(hass, runtime, dict(call.data))
            if result.get("changed")
            else None
        )
        return result
    except (SpotifyApiError, SpotifyWebApiError, SpotifyZeroconfApiError) as err:
        raise ServiceValidationError(str(getattr(err, "Message", err))) from err
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Spotify Connect activation failed")
        raise HomeAssistantError(str(err)) from err


def _spotify_access_token(runtime: RuntimeData) -> str | None:
    token = runtime.session.token or {}
    value = token.get("access_token")
    if value:
        return str(value)
    auth_token = getattr(runtime.client, "AuthToken", None)
    value = getattr(auth_token, "AccessToken", None)
    return str(value) if value else None


def _activate_connect_accesstoken_sync(runtime: RuntimeData, data: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Activate a Spotify Connect ZeroConf device that advertises TokenType=accesstoken.

    This is deliberately Connect-only: it posts to the advertised _spotify-connect._tcp
    addUser endpoint and never resolves the device name through SpotifyConnectDirectory,
    so LG devices that also advertise non-Connect cannot be routed into non-Connect.
    """
    access_token = _spotify_access_token(runtime)
    if not access_token:
        return {
            "changed": False,
            "activation_method": "zeroconf_connect_accesstoken",
            "reason": "missing_oauth_access_token",
            "connect_result": None,
        }

    scheme = "https" if bool(data.get(ATTR_USE_SSL, False)) else "http"
    host = data[ATTR_HOST]
    port = int(data.get(ATTR_PORT, DEFAULT_PORT))
    cpath = data.get(ATTR_CPATH, DEFAULT_CPATH)
    version = data.get(ATTR_VERSION, DEFAULT_ZC_VERSION)
    endpoint = f"{scheme}://{host}:{port}{cpath}"

    login_id = str(runtime.entry.data.get(CONF_ID) or getattr(runtime.client.UserProfile, "Id", "") or "")
    remote_name = info.get("RemoteName") or info.get("DeviceName") or info.get("Name")
    device_id = info.get("DeviceId") or info.get("deviceId")

    payload = {
        "action": "addUser",
        "version": version or DEFAULT_ZC_VERSION,
        "tokenType": "accesstoken",
        "clientKey": "",
        "loginId": login_id,
        "userName": login_id,
        "blob": access_token,
    }
    if remote_name:
        payload["deviceName"] = remote_name
    if device_id:
        payload["deviceId"] = device_id

    _LOGGER.debug(
        "Issuing Connect-only accesstoken addUser host=%s port=%s cpath=%s device=%s device_id=%s",
        host,
        port,
        cpath,
        remote_name,
        device_id,
    )
    response = requests.post(
        endpoint,
        timeout=10,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Connection": "close"},
        data=payload,
    )
    try:
        response_data = response.json()
    except Exception:  # noqa: BLE001
        response_data = {"raw": response.text}

    status = response_data.get("status") or response_data.get("Status")
    status_string = response_data.get("statusString") or response_data.get("StatusString")
    ok = response.status_code == 200 and str(status) in ("101", "0", "OK")
    return {
        "changed": bool(ok),
        "activation_method": "zeroconf_connect_accesstoken",
        "http_status": response.status_code,
        "status": status,
        "status_string": status_string,
        "connect_result": response_data,
        "reason": None if ok else "zeroconf_adduser_failed",
    }


def _activate_device_sync(runtime: RuntimeData, data: dict[str, Any]) -> dict[str, Any]:
    """Activate one advertised Spotify Connect ZeroConf endpoint.

    No name resolution, no non-Connect, no Spotify Connect token import.  The function
    uses only the host/port/cpath from _spotify-connect._tcp discovery.
    """
    before = None
    if bool(data.get(ATTR_VERIFY_BEFORE, True)):
        before = _verify_device_sync(runtime, data)
        if before.get("available_in_spotify"):
            return {
                "changed": False,
                "reason": "already_available_in_spotify",
                "activation_method": "none",
                "before": before,
                "connect_result": None,
                "after": before,
                "verified": True,
            }

    info_obj = _make_zconn(runtime, data).GetInformation()
    info_dict = _to_dict(info_obj)
    token_type = str(info_dict.get("TokenType") or "").lower()
    model = str(info_dict.get("ModelDisplayName") or "").lower()

    if model in {"librespot", "go-librespot"}:
        response = {
            "changed": False,
            "reason": "requires_librespot_credentials_file",
            "activation_method": "zeroconf_connect",
            "token_type": token_type,
            "before": before,
            "info": info_dict,
            "connect_result": None,
            "after": before,
            "verified": False,
        }
    elif token_type == "accesstoken":
        response = _activate_connect_accesstoken_sync(runtime, data, info_dict)
        response.update({"before": before, "info": info_dict, "token_type": token_type})
    elif token_type == "authorization_code":
        # spotifywebapipython can exchange the OAuth token for an authorization_code
        # token for Connect.  It requires string username/password/login_id args,
        # but the password blob is not used as the final Connect blob in this flow.
        login_id = str(runtime.entry.data.get(CONF_ID) or getattr(runtime.client.UserProfile, "Id", "") or "")
        try:
            connect_result = _make_zconn(runtime, data).Connect(login_id, "", login_id, float(data.get(ATTR_DELAY, DEFAULT_DELAY)))
            response = {
                "changed": True,
                "activation_method": "zeroconf_connect_authorization_code",
                "before": before,
                "info": info_dict,
                "token_type": token_type,
                "connect_result": _to_dict(connect_result),
            }
        except Exception as err:  # noqa: BLE001
            response = {
                "changed": False,
                "reason": _spotify_api_error_reason(err),
                "activation_method": "zeroconf_connect_authorization_code",
                "before": before,
                "info": info_dict,
                "token_type": token_type,
                "connect_result": None,
                "error": str(getattr(err, "Message", err)),
            }
    else:
        response = {
            "changed": False,
            "reason": "requires_spotify_connect_username_password",
            "message": "This Spotify Connect device advertises a token type that spotifywebapipython handles via username/password blob credentials. This integration does not request a Spotify password.",
            "activation_method": "zeroconf_connect",
            "token_type": token_type,
            "before": before,
            "info": info_dict,
            "connect_result": None,
            "after": before,
            "verified": False,
        }

    if bool(data.get(ATTR_VERIFY_AFTER, True)) and response.get("changed"):
        import time
        timeout = max(0, int(data.get(ATTR_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT)))
        end = time.monotonic() + timeout
        after = None
        while True:
            after = _verify_device_sync(runtime, data)
            if after.get("available_in_spotify") or time.monotonic() >= end:
                break
            time.sleep(1)
        response["after"] = after
        response["verified"] = bool(after and after.get("available_in_spotify"))
    elif "after" not in response:
        response["after"] = before
        response["verified"] = False
    return response


async def _handle_disconnect_device(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Disconnect one Spotify Connect device from the current Spotify account/session."""
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))

    _LOGGER.debug(
        "Disconnecting Spotify Connect device host=%s port=%s cpath=%s ssl=%s verify_before=%s verify_after=%s",
        call.data[ATTR_HOST],
        call.data.get(ATTR_PORT),
        call.data.get(ATTR_CPATH),
        call.data.get(ATTR_USE_SSL),
        call.data.get(ATTR_VERIFY_BEFORE),
        call.data.get(ATTR_VERIFY_AFTER),
    )

    try:
        async with runtime.operation_lock:
            # Disconnect is a local ZeroConf operation. Do not re-seed SpotifyClient
            # before touching the device; that can restart the library directory task
            # and make some devices briefly refuse their advertised ZeroConf port.
            if bool(call.data.get(ATTR_VERIFY_BEFORE, False)) or bool(call.data.get(ATTR_VERIFY_AFTER, False)):
                await _refresh_client_token(hass, runtime)
            result = await hass.async_add_executor_job(_disconnect_device_sync, runtime, dict(call.data))
        if result.get("changed"):
            result["auto_discovery_suspend"] = _suspend_auto_discovery(
                runtime,
                call.data.get(ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS, DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS),
            )
        else:
            result["auto_discovery_suspend"] = None
        result["native_spotify_refresh"] = (
            await _maybe_refresh_native_spotify(hass, runtime, dict(call.data))
            if result.get("changed")
            else None
        )
        return result
    except (SpotifyApiError, SpotifyWebApiError, SpotifyZeroconfApiError) as err:
        raise ServiceValidationError(str(getattr(err, "Message", err))) from err
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Spotify Connect disconnect failed")
        raise HomeAssistantError(str(err)) from err


def _disconnect_device_sync(runtime: RuntimeData, data: dict[str, Any]) -> dict[str, Any]:
    """Call ZeroConf Disconnect and optionally verify that Web API no longer lists the device.

    Do not call getInfo as a preflight. Several real devices advertise a Spotify
    Connect service but briefly refuse the local port. Disconnect should be a
    best-effort resetUsers call, not a service-breaking getInfo probe.
    """
    zconn = _make_zconn(runtime, data)
    before = None
    if bool(data.get(ATTR_VERIFY_BEFORE, False)):
        before = _verify_device_sync(runtime, data)

    response: dict[str, Any] = {
        "changed": False,
        "before": before,
        "disconnect_result": None,
        "after": None,
    }

    if before is not None and not before.get("available_in_spotify"):
        response["skipped"] = True
        response["reason"] = "already_not_available_in_spotify"
        response["verified"] = True
        return response

    try:
        disconnect_result = zconn.Disconnect(float(data.get(ATTR_DELAY, DEFAULT_DELAY)))
        response["changed"] = True
        response["disconnect_result"] = _to_dict(disconnect_result)
    except Exception as err:  # noqa: BLE001
        response["error"] = str(err)
        response["verified"] = False
        return response

    if bool(data.get(ATTR_VERIFY_AFTER, True)):
        import time

        timeout = max(0, int(data.get(ATTR_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT)))
        end = time.monotonic() + timeout
        after = None
        while True:
            after = _verify_device_sync(runtime, data)
            # For disconnect, success means Spotify Web API no longer exposes it as an available device.
            if not after.get("available_in_spotify") or time.monotonic() >= end:
                break
            time.sleep(1)
        response["after"] = after
        response["verified"] = bool(after and not after.get("available_in_spotify"))
    return response


async def _handle_disconnect_all(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Discover and disconnect all currently visible local Spotify Connect devices."""
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    timeout = int(call.data.get(ATTR_TIMEOUT, DEFAULT_TIMEOUT))
    results: list[dict[str, Any]] = []
    changed_any = False
    verified_any = False

    async with runtime.operation_lock:
        devices = await hass.async_add_executor_job(_discover_sync, runtime, timeout)

        for item in devices:
            params = _params_from_discovery(item)
            if params is None:
                results.append({"discovery": item, "skipped": True, "reason": "missing_host_port_or_cpath"})
                continue
            data = {
                **dict(call.data),
                ATTR_HOST: params[ATTR_HOST],
                ATTR_PORT: params[ATTR_PORT],
                ATTR_CPATH: params[ATTR_CPATH],
                ATTR_VERSION: params.get(ATTR_VERSION, DEFAULT_ZC_VERSION),
                ATTR_USE_SSL: params.get(ATTR_USE_SSL, False),
            }
            try:
                if bool(data.get(ATTR_VERIFY_BEFORE, False)) or bool(data.get(ATTR_VERIFY_AFTER, False)):
                    await _refresh_client_token(hass, runtime)
                result = await hass.async_add_executor_job(_disconnect_device_sync, runtime, data)
                changed_any = changed_any or bool(result.get("changed") and result.get("disconnect_result") is not None)
                verified_any = verified_any or bool(result.get("verified"))
                results.append({"discovery": item, "disconnect": result})
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Disconnect failed for discovered Spotify Connect device: %s", item)
                results.append({"discovery": item, "error": str(err)})

    response: dict[str, Any] = {
        "count": len(results),
        "changed_any": changed_any,
        "verified_any": verified_any,
        "results": results,
    }
    if changed_any:
        response["auto_discovery_suspend"] = _suspend_auto_discovery(
            runtime,
            call.data.get(ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS, DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS),
        )
    else:
        response["auto_discovery_suspend"] = None
    response["native_spotify_refresh"] = (
        await _maybe_refresh_native_spotify(hass, runtime, dict(call.data))
        if changed_any
        else None
    )
    return response


async def _handle_run_auto_cycle(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    return await _async_run_auto_cycle(hass, runtime, reason="manual_service")


async def _async_run_auto_cycle(hass: HomeAssistant, runtime: RuntimeData, *, reason: str) -> dict[str, Any]:
    """Discover local Spotify Connect devices, verify sources, activate only missing ones."""
    if runtime.auto_lock.locked():
        return {"started": False, "reason": "cycle_already_running"}

    if reason != "manual_service" and runtime.auto_suspended_until > _now_monotonic():
        remaining = round(runtime.auto_suspended_until - _now_monotonic(), 1)
        return {"started": False, "reason": "auto_discovery_suspended_after_disconnect", "remaining_seconds": remaining}

    async with runtime.auto_lock:
        async with runtime.operation_lock:
            await _refresh_client_token(hass, runtime)
            timeout = max(1, _option_int(runtime.entry, CONF_DISCOVERY_TIMEOUT, DEFAULT_DISCOVERY_TIMEOUT))
            verify_timeout = max(0, _option_int(runtime.entry, CONF_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT))
            _LOGGER.debug("Starting auto cycle reason=%s timeout=%s verify_timeout=%s", reason, timeout, verify_timeout)

            devices = await hass.async_add_executor_job(_discover_sync, runtime, timeout)
            results: list[dict[str, Any]] = []
            changed_any = False
            verified_any = False

            for item in devices:
                params = _params_from_discovery(item)
                if params is None:
                    results.append({"discovery": item, "action": "skipped", "reason": "missing_host_port_or_cpath"})
                    continue

                data = {
                    ATTR_HOST: params[ATTR_HOST],
                    ATTR_PORT: params[ATTR_PORT],
                    ATTR_CPATH: params[ATTR_CPATH],
                    ATTR_VERSION: params.get(ATTR_VERSION, DEFAULT_ZC_VERSION),
                    ATTR_USE_SSL: params.get(ATTR_USE_SSL, False),
                    ATTR_VERIFY_BEFORE: True,
                    ATTR_VERIFY_AFTER: True,
                    ATTR_VERIFY_TIMEOUT: verify_timeout,
                    ATTR_DELAY: DEFAULT_DELAY,
                }
                try:
                    verify = await hass.async_add_executor_job(_verify_device_sync, runtime, data)
                    if verify.get("available_in_spotify"):
                        verified_any = True
                        results.append({"discovery": item, "action": "none", "reason": "already_available_in_spotify", "verify": verify})
                        continue

                    activation = await hass.async_add_executor_job(_activate_device_sync, runtime, data)
                    changed_any = changed_any or bool(activation.get("changed") and activation.get("connect_result") is not None)
                    verified_any = verified_any or bool(activation.get("verified"))
                    results.append({"discovery": item, "action": "activate", "activation": activation})
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Auto activation failed for discovered Spotify Connect device: %s", item)
                    results.append({"discovery": item, "action": "error", "error": str(err)})

        refresh_result = None
        if changed_any and _option_bool(runtime.entry, CONF_RELOAD_NATIVE_SPOTIFY, DEFAULT_RELOAD_NATIVE_SPOTIFY):
            refresh_result = await _refresh_native_spotify_entries(
                hass,
                delay=DEFAULT_RELOAD_DELAY,
            )

        response = {
            "started": True,
            "reason": reason,
            "discovered_count": len(devices),
            "changed_any": changed_any,
            "verified_any": verified_any,
            "results": results,
            "native_spotify_refresh": refresh_result,
        }
        runtime.last_auto_result = response
        _LOGGER.debug("Finished auto cycle: changed_any=%s verified_any=%s discovered_count=%s", changed_any, verified_any, len(devices))
        return response


async def _handle_activate_all(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    runtime = _get_runtime(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    timeout = int(call.data.get(ATTR_TIMEOUT, DEFAULT_TIMEOUT))
    results: list[dict[str, Any]] = []
    changed_any = False
    verified_any = False

    async with runtime.operation_lock:
        await _refresh_client_token(hass, runtime)
        devices = await hass.async_add_executor_job(_discover_sync, runtime, timeout)
        for item in devices:
            params = _params_from_discovery(item)
            if params is None:
                results.append({"discovery": item, "skipped": True, "reason": "missing_host_port_or_cpath"})
                continue
            data = {
                **dict(call.data),
                ATTR_HOST: params[ATTR_HOST],
                ATTR_PORT: params[ATTR_PORT],
                ATTR_CPATH: params[ATTR_CPATH],
                ATTR_VERSION: params.get(ATTR_VERSION, DEFAULT_ZC_VERSION),
                ATTR_USE_SSL: params.get(ATTR_USE_SSL, False),
            }
            try:
                result = await hass.async_add_executor_job(_activate_device_sync, runtime, data)
                changed_any = changed_any or bool(result.get("changed") and result.get("connect_result") is not None)
                verified_any = verified_any or bool(result.get("verified"))
                results.append({"discovery": item, "activation": result})
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Activation failed for discovered Spotify Connect device: %s", item)
                results.append({"discovery": item, "error": str(err)})

    response = {"count": len(results), "changed_any": changed_any, "verified_any": verified_any, "results": results}
    response["native_spotify_refresh"] = (
        await _maybe_refresh_native_spotify(hass, runtime, dict(call.data))
        if changed_any
        else None
    )
    return response

def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _property_value(item: dict[str, Any], *names: str) -> Any:
    """Return a value from spotifywebapipython discovery Properties.

    Discovery results may expose ZeroConf TXT records both as flattened keys
    (for example SpotifyConnectCPath) and as a list of {Name, Value} dicts.
    """
    wanted = {name.lower() for name in names}
    props = item.get("Properties")
    if isinstance(props, dict):
        for key, value in props.items():
            if str(key).lower() in wanted and value not in (None, ""):
                return value
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            name = prop.get("Name") or prop.get("name")
            if name is not None and str(name).lower() in wanted:
                value = prop.get("Value") if "Value" in prop else prop.get("value")
                if value not in (None, ""):
                    return value
    return None


def _params_from_discovery(item: dict[str, Any]) -> dict[str, Any] | None:
    host = _first_present(item, "HostIpAddress", "HostIPv4Address", "hostIpAddress", "host_ipv4_address", "IPAddress", "IpAddress")
    port = _first_present(item, "HostIpPort", "HostPort", "Port", "port", "hostIpPort")
    cpath = (
        _first_present(item, "SpotifyConnectCPath", "CPath", "cpath", "Cpath", "cPath")
        or _property_value(item, "CPath")
    )
    version = (
        _first_present(item, "SpotifyConnectVersion", "Version", "VERSION", "version")
        or _property_value(item, "VERSION", "Version")
        or DEFAULT_ZC_VERSION
    )
    use_ssl = _first_present(item, "UseSSL", "UseSsl", "useSSL", "use_ssl")
    if use_ssl is None:
        add_user_endpoint = _first_present(item, "ZeroconfApiEndpointAddUser")
        use_ssl = str(add_user_endpoint).lower().startswith("https://") if add_user_endpoint else False
    if host is None or port is None or cpath is None:
        _LOGGER.debug(
            "Skipping discovered Spotify Connect device because required fields are missing: host=%s port=%s cpath=%s keys=%s",
            host,
            port,
            cpath,
            sorted(item.keys()),
        )
        return None
    return {
        ATTR_HOST: str(host),
        ATTR_PORT: int(port),
        ATTR_CPATH: str(cpath),
        ATTR_VERSION: str(version),
        ATTR_USE_SSL: bool(use_ssl),
    }
