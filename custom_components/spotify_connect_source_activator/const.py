from __future__ import annotations

DOMAIN = "spotify_connect_source_activator"
NAME = "Spotify Connect Source Activator"
VERSION = "1.4.2"

CONF_RELOAD_NATIVE_SPOTIFY = "refresh_native_spotify_after_activation"
CONF_AUTO_DISCOVERY_ENABLED = "auto_discovery_enabled"
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_DISCOVERY_TIMEOUT = "discovery_timeout"
CONF_VERIFY_TIMEOUT = "verify_timeout"

SERVICE_DISCOVER = "discover"
SERVICE_GET_INFO = "get_info"
SERVICE_ACTIVATE_DEVICE = "activate_device"
SERVICE_ACTIVATE_ALL = "activate_all"
SERVICE_DISCONNECT_DEVICE = "disconnect_device"
SERVICE_DISCONNECT_ALL = "disconnect_all"
SERVICE_VERIFY_DEVICE = "verify_device"
SERVICE_REFRESH_NATIVE_SPOTIFY = "refresh_native_spotify"
SERVICE_RUN_AUTO_CYCLE = "run_auto_cycle"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TIMEOUT = "timeout"
ATTR_HOST = "host_ipv4_address"
ATTR_PORT = "host_ip_port"
ATTR_CPATH = "cpath"
ATTR_VERSION = "version"
ATTR_USE_SSL = "use_ssl"
ATTR_VERIFY_BEFORE = "verify_before"
ATTR_VERIFY_AFTER = "verify_after"
ATTR_VERIFY_TIMEOUT = "verify_timeout"
ATTR_DELAY = "delay"
ATTR_RELOAD_NATIVE_SPOTIFY = "refresh_native_spotify"
ATTR_SPOTIFY_CONFIG_ENTRY_ID = "spotify_config_entry_id"
ATTR_RELOAD_DELAY = "refresh_delay"
ATTR_SUSPEND_AUTO_DISCOVERY_SECONDS = "suspend_auto_discovery_seconds"

DEFAULT_AUTO_DISCOVERY_ENABLED = False
DEFAULT_SCAN_INTERVAL_MINUTES = 10
DEFAULT_DISCOVERY_TIMEOUT = 5
DEFAULT_TIMEOUT = 5
DEFAULT_PORT = 8200
DEFAULT_CPATH = "/zc"
DEFAULT_ZC_VERSION = "1.0"
DEFAULT_DELAY = 0.5
DEFAULT_VERIFY_TIMEOUT = 8
DEFAULT_RELOAD_DELAY = 2.0
DEFAULT_RELOAD_NATIVE_SPOTIFY = True
DEFAULT_SUSPEND_AUTO_DISCOVERY_SECONDS = 120
NATIVE_SPOTIFY_DOMAIN = "spotify"

# Broad Spotify OAuth consent set, plus `streaming` for Connect-device control paths
# Existing OAuth tokens created by older versions must be reauthorized after this list changes.
SPOTIFY_SCOPES = [
    "playlist-modify-private",
    "playlist-modify-public",
    "playlist-read-collaborative",
    "playlist-read-private",
    "streaming",
    "ugc-image-upload",
    "user-follow-modify",
    "user-follow-read",
    "user-library-modify",
    "user-library-read",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-email",
    "user-read-playback-position",
    "user-read-playback-state",
    "user-read-private",
    "user-read-recently-played",
    "user-top-read",
]
