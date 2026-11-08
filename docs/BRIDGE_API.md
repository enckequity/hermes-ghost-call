# Ghost Call shared bridge API, v1

The contract between the Ghost Call plugin in each workspace and one shared, multi-tenant
bridge. It follows the hermes-phone bridge contract: a bearer workspace token, a versioned
`/v1` prefix, https except on loopback, no redirects, cursor polling, idempotent `PUT`s and
every safety rule enforced on the bridge. When `GHOST_CALL_BRIDGE_URL` or
`GHOST_CALL_BRIDGE_TOKEN` is unset, the plugin works as upstream does (co-located bridge, shared
SQLite) and none of this is used.

Client: `hermes_ghost_call/remote.py`. Server: `_add_shared_routes` in
`hermes_ghost_call/bridge/app.py`. Tests: `tests/test_shared_bridge.py`.

## Configuration

Plugin (workspace):

| Env | Meaning |
|---|---|
| `GHOST_CALL_BRIDGE_URL` | Bridge base URL. Must be `https`; `http` only for `127.0.0.1`, `localhost`, `::1`. |
| `GHOST_CALL_BRIDGE_TOKEN` | Workspace token (secret, read through the profile-scoped lookup). |
| `OPENROUTER_API_KEY` | The workspace's own key for the hint and summary model. Sent once per session. |

Bridge:

| Env | Meaning |
|---|---|
| `GHOST_CALL_BRIDGE_TENANTS` | JSON: `{"<sha256 hex of a token>": {"workspace": "<id>", "hint_base_url": "https://...", "hint_model": "...", "daily_session_limit": 20}}`. Malformed JSON stops the bridge at start. |
| `PUBLIC_BASE_URL` | Where the bridge serves pages and webhooks (`https://`). |
| `OPENAI_API_KEY` | The bridge's own key, for transcription and earpiece speech only. |
| `GHOST_CALL_TELNYX_API_KEY`, `GHOST_CALL_TELNYX_CONNECTION_ID` | Phone mode: Call Control credentials. |
| `GHOST_CALL_TELNYX_PUBLIC_KEY` | Phone mode: base64 Ed25519 key that signs webhooks. Unset means every webhook is refused. |
| `GHOST_CALL_CALLER_NUMBER` | Phone mode: caller ID on both legs (E.164). |
| `HERMES_GHOST_ALLOW_ALL=1` or `HERMES_GHOST_TEST_ALLOWLIST` | Dial allowlist, checked on the bridge. Empty refuses every number. |

## Auth and isolation

Every request sends `Authorization: Bearer <token>`. The bridge hashes it and finds the
workspace in `GHOST_CALL_BRIDGE_TENANTS`; an unknown token gets `401`. No request names a
workspace. Sessions are keyed by `(workspace, session id)`, so two workspaces may use the same
id, and an id that belongs to another workspace answers `404`, exactly as if it did not exist.

Errors: `{"error": {"code": "...", "message": "short, no secrets, no vendor names"}}`.

## Endpoints

### `GET /v1/health`
`200 {"ok": true, "phone": <bool>}`.

### `PUT /v1/sessions/{session_id}`: start a session
`session_id` is the plugin's local id (`[A-Za-z0-9_-]{1,64}`).

```json
{"goal": "lower my bill, target $60", "callee_label": "Northwind", "mode": "web|room|phone",
 "config": {"hint_mode": "auto", "max_duration_s": 900, "user_name": "Winch"},
 "numbers": {"you": "+12125550123", "them": "+13105550199"},
 "hint": {"api_key": "<workspace key>", "model": ""}}
```

- `201` new, `200` repeat of the same id (same body while the bridge has it in memory, else
  `links: {}`): `{"session_id", "state": "created", "mode", "links": {"you", "them"},
  "expires_in": 600}`. Room mode has only `you`; phone mode has no links (the bridge rings).
- The bridge clamps `config` with the plugin's own rules, keeps the 2.5 s hint timeout, turns
  `store_transcripts` off (the transcript lives in memory for the summary only) and takes the
  hint model from `hint.model`, else the tenant's `hint_model`. The hint base URL comes only from
  the tenant config.
- `hint.api_key` is kept in the bridge's memory for this session and never written to disk. It
  is dropped when the session ends. A restarted bridge has no key, so a session in flight then
  gets no more hints and a fallback summary.
- `422` codes: `bad_mode`, `goal_required`, `bad_id`, `phone_unavailable`, `session_active`
  (one active session per workspace), `daily_limit` (per workspace), `number_refused`
  (emergency, premium, not E.164, or not allowed). `502 dial_failed` when the first ring fails.

### `GET /v1/sessions/{session_id}/updates?cursor=<n>`
Polled about once a second by the ticker watcher until the summary has arrived.

```json
{"cursor": "2",
 "session": {"state": "created|live|ended", "end_reason": null, "consented": true,
             "usage": {"stt_seconds": 61.2, "tts_chars": 87}, "summary": null, "hint_count": 2},
 "hints": [{"n": 1, "t": 4.0, "text": "they said $70 → counter"}]}
```

`hints` holds only hints after `cursor`. The plugin mirrors each one by its bridge number `n`
(a replayed page adds nothing), mirrors the state, `usage` and the write-once summary into its
local store, and the ticker, summary and tools then work unchanged. `401`/`403`/`404` ends the
local session (`bridge_rejected`); other failures for 120 s end it as `bridge_unreachable`.

`session.usage` is the metering record: seconds of audio sent to transcription and characters
spoken by text to speech (whispers and the phone disclosure).

### `PUT /v1/sessions/{session_id}/commands/{command_id}`
`{"kind": "speak", "n": 3}` whispers hint 3 (or the latest when `n` is null) to the user only.
A repeated `command_id` is a no-op. `409` when the session is not live. Any other `kind` is
`422`: Intervene (Ghost speaking to the other party) does not exist on the shared bridge, and the
plugin refuses it too.

### `POST /v1/sessions/{session_id}/hangup`
Body `{}`. Idempotent; the summary then arrives through `updates`.

## Consent

- **Web and room mode.** The user's page shows "Let the other person know this call is being
  transcribed." and a confirm button. The bridge feeds no audio to transcription before that
  confirm (`consent` from the user's page only; the other party's page cannot send it). Web mode
  still relays the call audio between the two pages before it.
- **Phone mode.** When the other party answers, the bridge speaks "This call is being
  transcribed by an assistant." to that leg alone. Only when that playback has ended does it
  bridge the two legs and start forking audio, and the media socket refuses any stream before
  then. This cannot be turned off.

## Phone mode on Telnyx

1. Ring the customer (`POST /calls`, caller ID `GHOST_CALL_CALLER_NUMBER`).
2. On `call.answered` for the customer, ring the other party.
3. On `call.answered` for the other party, `speak` the disclosure on that leg.
4. On `call.speak.ended` of the disclosure, `bridge` the legs, then `streaming_start` on each
   leg with `stream_track: inbound_track` and a one-time media token in the URL.
5. Whispers: `speak` on the customer's leg with `target_legs: self`. The live two-phone check in
   enckequity/teamshift#1090 slice 3 must confirm the other party hears nothing.
6. `call.hangup` on either leg ends the session and hangs up the other leg.

**Why one stream per leg, not one `both_tracks` stream.** Each stream then carries exactly one
speaker, so transcripts are labelled by construction, the same way as web mode. With one
`both_tracks` stream on the customer's leg, its outbound track would also carry the whisper
played to the customer, and the whisper would be transcribed as if the other party had said
it.

Webhooks are verified with Ed25519 over `{telnyx-timestamp}|{body}` and refused when the
signature is missing, wrong or more than 300 s old, or when no public key is configured. Each
event must also carry a `client_state` naming a phone session and a leg whose stored call id
matches the event, so a replayed or misrouted event changes nothing.

## Versioning

The version is the path prefix `/v1`. Adding fields is not a breaking change, and both sides
ignore fields they do not know. Removing or renaming fields, or changing what they mean,
requires `/v2`.
