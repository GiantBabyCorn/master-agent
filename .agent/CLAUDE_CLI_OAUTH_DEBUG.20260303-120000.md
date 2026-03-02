# Claude CLI OAuth Login — Debug & Implementation Session

**Date**: 2026-03-03
**Status**: Partially working — code forwarding confirmed, root cause of rejection still being diagnosed

---

## Summary of Work

This session continued from a previous context where `claude_cli` OAuth login was broken. The
`claude.ai` "Authorize" button was returning 400 errors. We implemented a dual-strategy approach,
fixed several infra bugs in Docker/DB, and traced the PTY subprocess interaction down to the
controlling terminal level.

---

## Context: What Broke Before This Session

- `claude auth login` was used initially but froze waiting for interactive menu selection
- PKCE-via-claude.ai returned 400 (internal endpoint, some accounts unsupported)
- OAuth URL capture used a non-blocking `read()` race that expired before CLI printed the URL

---

## Dual Login Strategy

Configurable via `CLAUDE_LOGIN_METHOD` in `.env`:

| Method | Setting | How |
|---|---|---|
| PTY (default) | `auth_login` | `claude setup-token` in pseudo-terminal |
| API PKCE | `pkce_platform` | Build PKCE URL, exchange code at `platform.claude.com` |

### Why `claude setup-token` instead of `claude auth login`

`claude setup-token`:
- Goes directly to OAuth URL (no interactive menu)
- Shows URL then waits at `Paste code here if prompted >` prompt
- Designed for headless/non-browser environments
- Creates a long-lived token (`sk-ant-oat01-...`) instead of a session credential

### PKCE Parameters (from claude CLI source)

```
Auth URL:    https://platform.claude.com/oauth/authorize  (platform method)
             https://claude.ai/oauth/authorize             (default method)
Token URL:   https://platform.claude.com/v1/oauth/token
Redirect:    https://platform.claude.com/oauth/code/callback
Scopes:      user:profile user:inference user:sessions:claude_code user:mcp_servers
Client ID:   9d1c250a-e61b-44d9-88ed-5944d1962f5e  (overridable via CLAUDE_CODE_OAUTH_CLIENT_ID)
Extra param: code=true  (Anthropic-specific, always included)
Code format: {code}#{state}  — split on # to get just the code before token exchange
```

---

## Bug Fixes

### 1. PostgreSQL Enum Missing Values

**Error**: `invalid input value for enum providerkind: 'CLAUDE_CLI'`

**Root cause**: SQLAlchemy `create_all()` never alters existing enum types in PostgreSQL. The
`CLAUDE_CLI` and `ANTHROPIC_API` values added to Python's `ProviderKind` enum were not present in
the DB schema.

**Fix** (`app/main.py`):
```python
from sqlalchemy import text

def _migrate_enum_values() -> None:
    new_values = {"providerkind": ["CLAUDE_CLI", "ANTHROPIC_API"]}
    with engine.begin() as conn:
        for type_name, values in new_values.items():
            for value in values:
                conn.execute(text(
                    f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"
                ))

# Called in on_startup() after create_all()
_migrate_enum_values()
```

`ADD VALUE IF NOT EXISTS` is idempotent — safe to run on every startup.

---

### 2. Docker Root User Blocking `--dangerously-skip-permissions`

**Error**: `--dangerously-skip-permissions cannot be used with root/sudo privileges`

**Root cause**: Docker container was running as root (UID 0). Claude CLI hard-blocks this flag for
root to prevent accidental privileged operations.

**Fix** (`Dockerfile`):
```dockerfile
RUN useradd -m -u 1000 -s /bin/bash claude
WORKDIR /app
RUN chown claude:claude /app
COPY --chown=claude:claude pyproject.toml README.md ./
COPY --chown=claude:claude app ./app
USER claude
ENV PATH="/home/claude/.local/bin:${PATH}"
ENV NPM_CONFIG_PREFIX=/home/claude/.local
RUN pip install --no-cache-dir -e .
```

Side effect: `npm install -g` now needs `NPM_CONFIG_PREFIX` to redirect from `/usr/local` to a
user-writable directory.

---

### 3. npm Global Install EACCES

**Error**: `EACCES: permission denied, mkdir '/usr/local/lib/node_modules'`

**Fix**: `ENV NPM_CONFIG_PREFIX=/home/claude/.local` in Dockerfile (see above)

---

### 4. PTY Process Exits Immediately (No Controlling Terminal)

**Symptom**: OAuth URL appeared in PTY output for ~0.5s, then process exited. Login flow appeared
"frozen."

**Root cause**: `claude setup-token` reads the pasted auth code via `/dev/tty` (masked secure
input). Without a controlling terminal attached to the process, the `open("/dev/tty")` call inside
the CLI fails and the process exits immediately.

**Fix** (`app/providers/_login_helper.py`):
```python
def _setup_ctty() -> None:
    """Set slave PTY fd 0 as controlling terminal so /dev/tty refers to it."""
    try:
        import fcntl as _fcntl2
        import termios as _termios2
        _fcntl2.ioctl(0, _termios2.TIOCSCTTY, 0)
    except OSError:
        pass  # Non-fatal

proc = subprocess.Popen(
    cmd_args,
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    close_fds=True,
    start_new_session=True,   # setsid() → child becomes session leader (no ctty)
    preexec_fn=_setup_ctty,   # TIOCSCTTY → slave PTY becomes controlling terminal
)
```

**Sequence**:
1. `start_new_session=True` → kernel calls `setsid()` in child process
2. Child is now a session leader with no controlling terminal
3. `preexec_fn` runs after fork but before exec → `TIOCSCTTY(fd=0)` attaches slave PTY as ctty
4. `open("/dev/tty")` inside the CLI now succeeds, pointing to the slave PTY
5. The PTY line discipline bridges reads/writes through the master fd held by our process

---

### 5. `wait_login()` Blocking Full Timeout on Bad Code

**Symptom**: After code was forwarded ("Code sent — waiting..."), login showed as failed after 5
minutes with no useful error in Telegram.

**Root cause**: `session.wait(timeout_sec)` blocks on `proc.wait()`, ignoring all PTY output
including the "Invalid code. Press Enter to retry." prompt shown when the code is wrong.

**Fix** — polling loop with pattern detection (`app/providers/claude_cli_provider.py`):
```python
if isinstance(session, LoginSession):
    deadline = _time.monotonic() + timeout_sec
    while _time.monotonic() < deadline:
        rc = session.proc.poll()
        if rc is not None:
            return rc == 0  # Fast exit on process termination
        out = session.output_so_far()
        if "press enter to retry" in out.lower():
            session.kill()  # Don't wait 5 min for a retry prompt
            logger.warning("claude_cli: code rejected. PTY output:\n%s", out[-2000:])
            return False
        _time.sleep(0.5)
    session.kill()
    return False
```

**Also added**: PTY output appended to Telegram failure message for diagnostics
(`app/channels/telegram/dispatcher.py`, `_wait_and_retry()`).

---

## Current State (End of Session)

### What's Working
- Container starts as non-root user `claude` — `--dangerously-skip-permissions` accepted
- DB enum migration runs on startup — no more enum errors
- `claude setup-token` spawns in PTY with full controlling terminal — process stays alive
- OAuth URL is captured and sent to Telegram as a URL button
- User pastes code → code is forwarded to PTY stdin (`send_code()` confirmed working)
- `wait_login()` detects "Press Enter to retry" quickly instead of blocking 5 minutes

### What's Not Yet Confirmed
- **End-to-end login success**: The code IS being forwarded but we haven't observed a successful
  `rc == 0` exit from `claude setup-token`. Possible causes:
  1. Code format issue — the PTY may need `\n` not `\r` as terminator (or vice versa)
  2. The pasted value might include extra chars (e.g., `{code}#{state}` needs `#` stripping for
     `setup-token` but not for PKCE — the CLI handles it internally)
  3. Timing — code sent too fast before CLI is ready at the prompt
  4. Terminal mode — CLI might be in raw mode, `\r` needed (current impl uses `\r`); or
     canonical mode, `\n` needed

### Next Debugging Steps
1. **Check PTY output in failure message** — the new diagnostic text in Telegram shows last 6
   lines of PTY output; this should reveal exactly what the CLI received/rejected
2. **Try PKCE method** — set `CLAUDE_LOGIN_METHOD=pkce_platform` in `.env`; completely bypasses
   PTY, does pure HTTP token exchange; if this works, CLI method can be debugged separately
3. **Token storage question** — `claude setup-token` may suggest `export CLAUDE_CODE_OAUTH_TOKEN=`
   instead of writing `~/.claude/.credentials.json`. If so, we need to capture that env var from
   PTY output and inject it when running tasks.

---

## Files Modified This Session

| File | Change |
|------|--------|
| `app/providers/claude_cli_provider.py` | Dual login methods, PKCE URL build, polling `wait_login()` |
| `app/providers/_login_helper.py` | `start_new_session=True` + `_setup_ctty` preexec_fn |
| `app/main.py` | `_migrate_enum_values()` + `text` import from sqlalchemy |
| `Dockerfile` | Non-root `claude` user, `NPM_CONFIG_PREFIX` |
| `app/core/config.py` | `claude_code_oauth_client_id`, `claude_auth_use_platform`, `claude_login_method` |
| `app/channels/telegram/dispatcher.py` | PTY diagnostic output in failure message, labeled `/projects` sections |
| `.env` / `.env.example` | New settings sections for Claude CLI, GitHub, Scheduler |

---

## Key References

- Claude CLI source (inferred from behavior): `CLAUDE_CODE_OAUTH_TOKEN` env var, `setup-token` command
- PTY controlling terminal: `man 4 tty`, POSIX `setsid(2)`, `ioctl TIOCSCTTY`
- PostgreSQL `ALTER TYPE … ADD VALUE IF NOT EXISTS`: pg docs §8.7
