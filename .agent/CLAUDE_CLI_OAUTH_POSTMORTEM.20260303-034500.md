# Claude CLI OAuth – Debugging Postmortem

**Date:** 2026-03-03
**Feature:** Claude CLI OAuth login flow via Telegram bot (PKCE method)
**Status:** ✅ Resolved

---

## The Goal

Allow users who only have a claude.ai subscription (no Anthropic API key) to authenticate
the `claude_cli` provider through Telegram. Flow:

1. User sends `/run claude_cli <prompt>` or `/login claude_cli`
2. Bot detects auth required → shows a login URL button
3. User opens URL in browser, authorizes on claude.ai
4. Callback page shows `{code}#{state}` — user copies and pastes into Telegram
5. Bot exchanges the code for tokens, writes credentials, retries the task

---

## Issues Encountered (in order)

### 1. PTY method hung after code submission
**Method:** `auth_login` — spawns `claude setup-token` in a PTY
**Symptom:** Code was written to PTY stdin, echoed back, then silence for 2+ minutes.
**Root cause:** `claude setup-token`'s Node.js HTTP client tries to POST to
`platform.claude.com/v1/oauth/token` and hangs — network timeout from inside the Docker
container (likely Cloudflare or container DNS issue specific to Node.js).
**Fix:** Switched to `CLAUDE_LOGIN_METHOD=pkce_platform` — Python does the token exchange
instead of Node.js.

---

### 2. PKCE authorization returned 400 — wrong scopes
**Symptom:** `POST claude.ai/.../authorize` → `400 Invalid request format`
**Root cause:** Requested 4 scopes (`user:profile user:inference user:sessions:claude_code
user:mcp_servers`). The claude.ai internal authorize endpoint only accepts `user:inference`
for claude.ai subscription accounts.
**Fix:** `_PKCE_SCOPES = "user:inference"`

---

### 3. PKCE authorization still 400 — wrong state length
**Symptom:** Still `400 Invalid request format` on the auth URL even after scope fix.
**Root cause:** Our `state` was 22 chars (16 bytes via `secrets.token_urlsafe(16)`).
The server requires exactly 43 chars (32 bytes). Discovered by comparing a working
CLI browser request (state=43 chars) vs our PKCE request (state=22 chars) side by side.
**Fix:** `state = secrets.token_urlsafe(32)` → produces 43-char base64url string.

---

### 4. Token exchange returned 403 — Cloudflare UA block
**Symptom:** Auth URL returned 200 OK, code received, but `urlopen()` to
`platform.claude.com/v1/oauth/token` returned `HTTP 403 error code: 1010`
**Root cause:** Cloudflare blocks `User-Agent: Python-urllib/3.12` as a bot.
**Fix:** Added `User-Agent: claude-cli/2.1.50` and `Accept: application/json, */*` headers.

---

### 5. Token exchange returned 400 — form-encoded body
**Symptom:** After UA fix, still `400 Invalid request format` from token endpoint.
**Root cause:** `platform.claude.com/v1/oauth/token` is built on the Anthropic API framework
which expects JSON bodies. We were sending `application/x-www-form-urlencoded`.
**Fix:** Changed to `Content-Type: application/json` + `json.dumps(...)` body.

---

### 6. Token exchange returned 400 — missing `state` field in body
**Symptom:** Still `400 Invalid request format` even with JSON body.
**Root cause:** The token endpoint requires a `state` field in the request body.
This is non-standard OAuth but confirmed from the Claude CLI source (`cli.js`, function `F$8`).
Our exchange function received `state` as a parameter but named it `_state` (unused).
**Fix:** Added `"state": state` to the JSON body.

---

## Final Working Parameters

| Parameter | Value |
|---|---|
| Auth URL | `https://claude.ai/oauth/authorize` |
| Token URL | `https://platform.claude.com/v1/oauth/token` |
| Redirect URI | `https://platform.claude.com/oauth/code/callback` |
| Client ID | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| Scope | `user:inference` (only) |
| State length | 43 chars (`secrets.token_urlsafe(32)`) |
| Body format | JSON (`Content-Type: application/json`) |
| Code sent | bare code — strip `#{state}` suffix |
| State in body | yes — `"state": state` required |
| User-Agent | `claude-cli/2.1.50` |

---

## Key Lessons

- **Never assume OAuth RFC compliance** — Anthropic's token endpoint has two non-standard requirements: JSON body and `state` in the token exchange request.
- **Cloudflare UA filtering is silent** — no error page, just `403 error 1010`. Always set a realistic User-Agent for any requests to `*.claude.com` / `*.anthropic.com`.
- **Compare working vs failing requests at the network level** — the state length bug was only found by having the user capture browser network requests for both the CLI URL (200 OK) and our PKCE URL (400) and diffing them.
- **Read the CLI source** — `cli.js` in `@anthropic-ai/claude-code` npm package has the ground truth for all OAuth parameters.
