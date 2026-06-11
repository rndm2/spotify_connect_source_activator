# Spotify Connect Source Activator

Home Assistant custom integration that makes local **Spotify Connect** devices visible to the native Home Assistant Spotify integration.

It is intentionally Connect-only. It does not use Google Cast/Chromecast activation, does not import SpotifyPlus tokens, and does not store Spotify passwords.

## How it works

1. Discovers local `_spotify-connect._tcp` devices through mDNS/ZeroConf.
2. Calls the device's local `getInfo` endpoint to read its Connect `DeviceId`, `TokenType`, and display name.
3. Checks Spotify Web API available devices for that `DeviceId`.
4. If the device is missing and supports `TokenType=accesstoken`, posts `addUser` directly to the advertised Spotify Connect endpoint using the current OAuth access token.
5. Refreshes the native Home Assistant Spotify device coordinator so the native source list updates without reloading the integration.

## UI buttons

The integration creates three button entities:

- Run auto cycle
- Activate all discovered sources
- Refresh native Spotify sources

The same operations are also available as service actions for debugging.

## OAuth scopes

The integration requests broad Spotify scopes, including `user-read-playback-state` and `user-modify-playback-state`. If upgrading from an older version, reauthorize the integration so the token receives the new scopes.

## Limits

- `TokenType=default` devices usually require Spotify Connect username/password or a device password. This integration does not request those credentials and skips those devices with a diagnostic reason.
- Disconnect services are debug tools only. Some devices reject `resetUsers` or remain cached by Spotify for a while.
