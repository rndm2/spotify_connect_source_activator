from __future__ import annotations

from typing import Any, Callable

from spotifywebapipython import SpotifyClient
from zeroconf import Zeroconf


def create_spotify_client(
    *,
    token_storage_dir: str,
    token_storage_file: str,
    zeroconf_client: Zeroconf,
    token_updater: Callable[[], dict[str, Any]] | None = None,
) -> SpotifyClient:
    """Create an API client without library-owned discovery or Cast connections.

    Local Connect discovery belongs to the integration's explicit SpotifyDiscovery
    calls. The library's background directory also discovers Cast devices and is
    restarted whenever an OAuth token is assigned.
    """
    return SpotifyClient(
        tokenStorageDir=token_storage_dir,
        tokenStorageFile=token_storage_file,
        tokenUpdater=token_updater,
        zeroconfClient=zeroconf_client,
        spotifyConnectDiscoveryTimeout=0,
        spotifyConnectDirectoryEnabled=False,
    )
