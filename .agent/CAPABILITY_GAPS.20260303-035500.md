# Capability Gaps — Telegram ↔ Agent Flow

**Date:** 2026-03-03
**Context:** Observed after first successful `claude_cli` task attempt (e-commerce site, timed out)

---

## 1. Task Timeout — Large/Long-Running Tasks

**Current state:** `request_timeout_sec = 30` (hardcoded default in `app/core/config.py`).
The subprocess is killed after 30 seconds. Any non-trivial task (code generation, file creation,
multi-step work) will always fail.

**What's missing:**

- A configurable timeout per provider or per task
- Background/async execution — currently the subprocess blocks the handler thread
- Progress updates while the task is running (streaming or periodic "still working…" edits)

**Impact:** Any real Claude CLI task fails. The e-commerce site task needed minutes, got 30s.

---

## 2. Sending Files/Images TO Agents

**Current state:** Dispatcher only reads `message.text`. Telegram messages containing
`message.document`, `message.photo`, `message.video`, or `message.audio` are silently ignored.
`ProviderTaskRequest` has no field for attachments — only `prompt: str`.

**What's missing:**

- Dispatcher handler for incoming file/image messages
- Download the Telegram file (via `getFile` + CDN URL)
- Pass file paths or base64 content to the provider's `launch_task()`
- For `claude_cli`: pass as `--attachment` flag or write to the task workspace first

**Impact:** User cannot share designs, screenshots, existing code files, or data with agents.

---

## 3. Receiving Files FROM Agents

**Current state:** `send_telegram_document()` exists but only sends **text** (UTF-8 string)
as a `.md` file — used only for `/export`. There is no mechanism to:

- Send binary files (zip, images, PDFs) back to the user
- Detect files created by Claude in its workspace

`project_path=None` for all Telegram tasks, so even if Claude creates a zip file, the bot
has no idea where it is.

**What's missing:**

- Per-task dedicated workspace directory (e.g. `/workspaces/<task_id>/`)
- After task completes: scan workspace for output files, send each as Telegram document
- `send_telegram_document()` needs binary support (currently only handles `str`, not `bytes`)

**Impact:** User asked Claude to "pack into a Zip and send it" — completely unsupported.

---

## 4. OAuth Token Refresh

**Current state:** `refreshToken` is written to `~/.claude/.credentials.json` under
`claudeAiOauth.refreshToken` but **nothing ever reads or uses it**.
When the `accessToken` expires (typically ~1 hour), the next task will fail with
`AUTH_REQUIRED` and the user must go through the full PKCE login flow again.

**What's missing:**

- Token expiry check before task execution (read `expiresAt` from credentials)
- Automatic refresh: POST to `platform.claude.com/v1/oauth/token` with
`grant_type=refresh_token` + `refresh_token` + `client_id` (confirmed from CLI source)
- Write refreshed token back to credentials file

**Impact:** Silent re-auth every ~1 hour, bad UX.

---

## 5. No Per-Task Workspace

**Current state:** `project_path=None` for all Telegram-originated tasks.
Claude runs in whatever its default CWD is (likely `/app` inside the container).
Multiple concurrent tasks share the same directory — file conflicts are possible.

**What's missing:**

- Create `/workspaces/<task_id>/` before launching the task
- Pass it as `project_path` (already wired into `launch_task()` → `cwd=`)
- Clean up after the task (or archive for `/export`)

**Impact:** File outputs are unpredictable; concurrent tasks can corrupt each other.

---

## Priority Order (suggested)


| #   | Gap                                  | Effort | Impact                              |
| --- | ------------------------------------ | ------ | ----------------------------------- |
| 1   | Increase / make timeout configurable | Low    | Immediate — tasks actually complete |
| 2   | Per-task workspace                   | Low    | Enables file output                 |
| 3   | OAuth token refresh                  | Medium | Eliminates hourly re-auth           |
| 4   | Send files back to user              | Medium | Claude can deliver zip/images       |
| 5   | Accept files/images from user        | High   | Full two-way media flow             |


