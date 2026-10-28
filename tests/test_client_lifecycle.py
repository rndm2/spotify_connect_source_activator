"""Offline lifecycle regressions against the real spotifywebapipython dependency.

Run with: python -B -m unittest discover -s tests -v
HTTP, token storage, socket creation, and background thread starts are blocked.
"""
from __future__ import annotations

import ast
from contextlib import ExitStack
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from spotifywebapipython import SpotifyClient
    from zeroconf import Zeroconf
except ModuleNotFoundError as err:
    raise unittest.SkipTest("Install the integration's manifest requirements to run lifecycle tests") from err


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "spotify_connect_source_activator"


def load_tested_code():
    factory = {"__name__": "spotify_activator_client_test"}
    exec(compile((COMPONENT / "client.py").read_text(), "client.py", "exec"), factory)
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    names = {"_refresh_client_token", "_discover_sync", "_verify_device_sync", "_to_dict"}
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(functions) == len(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, "__init__.py", "exec"), namespace)
    return factory["create_spotify_client"], namespace


class SpotifyClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.create_client, self.integration = load_tested_code()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.zeroconf = Mock(spec=Zeroconf)
        self.token_updater = Mock()
        self.requests = []

        def auth_client(**kwargs):
            return SimpleNamespace(
                Session=SimpleNamespace(token={}),
                AuthorizationType=kwargs["authorizationType"],
                TokenProfileId=kwargs["tokenProfileId"],
            )

        def make_request(client, method, message, *args, **kwargs):
            self.requests.append((method, message.Uri))
            if message.Uri == "/me":
                message.ResponseData = {"id": "test-user", "display_name": "Test User", "email": "test@example.invalid", "product": "premium"}
            elif message.Uri == "/me/player/devices":
                message.ResponseData = {"devices": [{"id": "test-device", "name": "Test LG", "is_active": False, "type": "Speaker", "volume_percent": 8}]}
            else:
                raise AssertionError("Unexpected HTTP endpoint: " + message.Uri)
            return 200

        self.stack.enter_context(patch("spotifywebapipython.spotifyclient.os.makedirs"))
        self.stack.enter_context(patch("spotifywebapipython.spotifyclient.AuthClient", side_effect=auth_client))
        self.stack.enter_context(patch.object(SpotifyClient, "_LoadConfigurationData", return_value={}))
        self.stack.enter_context(patch.object(SpotifyClient, "_SaveConfigurationData", side_effect=AssertionError("Unexpected configuration write")))
        self.stack.enter_context(patch.object(SpotifyClient, "MakeRequest", new=make_request))
        self.start_thread = self.stack.enter_context(patch("threading.Thread.start", side_effect=AssertionError("Unexpected background worker")))
        self.open_socket = self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("Unexpected socket")))
        self.client = self.new_client()
        self.runtime = SimpleNamespace(
            client=self.client,
            session=SimpleNamespace(token=self.token(0), async_ensure_token_valid=AsyncMock()),
            implementation=SimpleNamespace(client_id="test-client"),
            last_token_key=None,
        )

        async def executor(operation, *args):
            return operation(*args)

        self.hass = SimpleNamespace(async_add_executor_job=executor)

    def new_client(self):
        client = self.create_client(
            token_storage_dir="/nonexistent-spotify-activator-lifecycle-test",
            token_storage_file="synthetic-tokens.json",
            zeroconf_client=self.zeroconf,
            token_updater=self.token_updater,
        )
        self.addCleanup(client.Dispose)
        return client

    @staticmethod
    def token(index):
        return {"access_token": "synthetic-token-" + str(index), "token_type": "Bearer", "expires_at": time.time() + 7200, "scope": "user-read-playback-state"}

    async def refresh(self):
        await self.integration["_refresh_client_token"](self.hass, self.runtime)

    def assert_no_background_activity(self):
        self.start_thread.assert_not_called()
        self.open_socket.assert_not_called()
        self.zeroconf.close.assert_not_called()

    async def test_repeated_oauth_refreshes_do_not_start_background_workers(self):
        for index in range(48):
            self.runtime.session.token = self.token(index)
            await self.refresh()
            self.assertEqual(self.client.AuthToken.AccessToken, "synthetic-token-" + str(index))
        self.assertEqual(self.requests.count(("GET", "/me")), 48)
        self.assertIs(self.client.ZeroconfClient, self.zeroconf)
        self.assertIs(self.client._TokenUpdater, self.token_updater)
        self.assert_no_background_activity()

    async def test_unchanged_token_does_not_repeat_profile_request(self):
        await self.refresh()
        await self.refresh()
        self.assertEqual(self.requests, [("GET", "/me")])
        self.assert_no_background_activity()

    async def test_setup_and_reload_cycles_do_not_leave_workers(self):
        for index in range(12):
            client = self.new_client()
            client.SetAuthTokenFromToken("test-client", self.token(index), None)
            self.assertEqual(client.UserProfile.Id, "test-user")
            client.Dispose()
            client.Dispose()
        self.assert_no_background_activity()

    async def test_known_device_verification_still_uses_web_api(self):
        await self.refresh()
        info = SimpleNamespace(DeviceId="test-device", ToDictionary=lambda: {"DeviceId": "test-device"})
        self.integration["_make_zconn"] = Mock(return_value=SimpleNamespace(GetInformation=lambda: info))
        result = self.integration["_verify_device_sync"](self.runtime, {"host": "synthetic-host"})
        self.assertTrue(result["available_in_spotify"])
        self.assertEqual(result["device_id"], "test-device")
        self.assertIn(("GET", "/me/player/devices"), self.requests)
        self.assert_no_background_activity()

    async def test_missing_device_is_not_reported_as_available(self):
        await self.refresh()
        info = SimpleNamespace(DeviceId="missing-device", ToDictionary=lambda: {"DeviceId": "missing-device"})
        self.integration["_make_zconn"] = Mock(return_value=SimpleNamespace(GetInformation=lambda: info))
        result = self.integration["_verify_device_sync"](self.runtime, {"host": "synthetic-host"})
        self.assertFalse(result["available_in_spotify"])
        self.assertIsNone(result["player_device"])
        self.assert_no_background_activity()

    async def test_explicit_connect_discovery_keeps_shared_zeroconf(self):
        discovery = Mock()
        discovery.DiscoveryResults = [{"DeviceName": "Test LG", "HostIpPort": 1234}]
        constructor = Mock(return_value=discovery)
        self.integration["SpotifyDiscovery"] = constructor
        result = self.integration["_discover_sync"](self.runtime, 3)
        self.assertEqual(result, discovery.DiscoveryResults)
        constructor.assert_called_once_with(self.zeroconf, printToConsole=False)
        discovery.DiscoverDevices.assert_called_once_with(3)
        self.assert_no_background_activity()


if __name__ == "__main__":
    unittest.main()
