# Provider Compatibility Decision

## Selected transport

Discord Gateway and Twitch EventSub WebSocket lifecycles use Deno 2.9.4 native
`WebSocket`; OAuth, REST, and Helix use native `fetch`. The runtime version is
pinned in `.tool-versions`. Provider calls remain behind typed interfaces so a
library can replace the transport without changing domain or repository code.

## Spike result

Discordeno and Twurple were considered, but neither is selected for the port.
QBot4K requires explicit database-backed ownership, raw frame control,
reconnect/resume behavior, capability checks before every outbound action, and
identical recorded payloads across the Python/Deno transition. Native Deno APIs
preserve those boundaries with fewer transitive runtime assumptions.

The recorded fixtures under `tests/fixtures/providers/` cover Discord hello,
dispatch sequence, heartbeat acknowledgement, reconnect, and resumable invalid
sessions, plus Twitch welcome, keepalive, reconnect URL, notification, and
revocation frames. `tests/deno_provider_protocol_test.ts` verifies strict,
fail-closed decoding. Fake-server and live provider behavior remain DF5-02
through DF5-05 work.