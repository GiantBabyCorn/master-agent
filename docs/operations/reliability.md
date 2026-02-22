# Reliability and Safety Notes

## Database Persistence

- Docker Compose uses named volume `postgres_data`.
- Do not run `docker compose down -v` unless you intentionally want to delete DB data.
- Run scheduled backups with `scripts/backup_db.sh`.

## Startup Migration Gate

- `DB_ENABLE_STARTUP_MIGRATION_GATE=true` checks baseline table presence before startup.
- `DB_AUTO_CREATE_TABLES=true` allows bootstrap table creation for early-stage development.
- Move to Alembic-managed migrations before production.

## Retry and Circuit Breaker

- Retry policy is configurable:
  - `RETRY_MAX_ATTEMPTS`
  - `RETRY_BASE_DELAY_MS`
- Circuit breaker controls repeated provider failures:
  - `CIRCUIT_BREAKER_FAIL_THRESHOLD`
  - `CIRCUIT_BREAKER_RECOVERY_SEC`

## Correlation IDs

- Every request is assigned a correlation ID via middleware.
- Returned in `x-correlation-id` response header.
- Included in logs for traceability.
