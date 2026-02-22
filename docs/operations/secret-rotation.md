# Secret Rotation Policy

## Scope

This policy applies to:

- `TELEGRAM_BOT_TOKEN`
- `CURSOR_CLOUD_API_KEY`
- `ANTHROPIC_API_KEY`
- `CODEX_API_KEY`

## Rotation Frequency

- Production: every 30 days
- Staging: every 60 days
- Development: as needed
- Immediate rotation on suspected leak

## Rotation Procedure

1. Create a new key in provider console.
2. Update runtime secret store and deployment environment.
3. Restart API process.
4. Verify `/readyz` and provider health endpoint.
5. Revoke old key after successful verification.

## Emergency Revocation

1. Disable leaked key immediately.
2. Rotate all affected downstream keys.
3. Review logs for suspicious requests.
4. Record incident in change log and event records.
