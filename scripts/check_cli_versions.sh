#!/usr/bin/env bash
set -euo pipefail

echo "== Master Agent CLI version check =="
echo

if ! docker compose ps >/dev/null 2>&1; then
  echo "docker compose is not available or project is not initialized."
  exit 1
fi

if ! docker compose ps master_agent_api >/dev/null 2>&1; then
  echo "master_agent_api service is not found in docker compose."
  exit 1
fi

run_in_api() {
  docker compose exec -T master_agent_api bash -lc "$1"
}

echo "[Cursor CLI]"
if run_in_api "command -v agent >/dev/null 2>&1"; then
  run_in_api "agent --version || true"
else
  echo "agent command not found"
fi
echo "Update source: https://cursor.com/docs/cli/overview"
echo "Changelog: https://cursor.com/changelog"
echo

echo "[Claude CLI]"
if run_in_api "command -v claude >/dev/null 2>&1"; then
  run_in_api "claude --version || true"
  echo "Tip: run 'claude update' inside container to update."
else
  echo "claude command not found"
fi
echo "Update source: https://docs.anthropic.com/en/docs/claude-code/setup"
echo

echo "[Codex CLI]"
if run_in_api "command -v codex >/dev/null 2>&1"; then
  run_in_api "codex --version || true"
else
  echo "codex command not found"
fi
echo "Update source: https://developers.openai.com/codex/cli/"
echo "Releases: https://github.com/openai/codex/releases"
echo

echo "[Reference]"
echo "If you changed install commands, rebuild API image:"
echo "  docker compose build --no-cache api"
echo "  docker compose up -d"
