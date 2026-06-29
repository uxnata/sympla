#!/usr/bin/env bash
# SessionStart hook: подготовить окружение и убедиться, что оффлайн self-test зелёный.
# Без сети к Sympla тест всё равно проходит (он на фикстурах).
set -u
cd "$(dirname "$0")/../.." || exit 0

python3 -m pip install -q -r requirements.txt 2>/dev/null || true

if python3 sympla_agent.py --selftest; then
  echo "[session_start] sympla_agent self-test: OK"
else
  echo "[session_start] sympla_agent self-test: FAIL — проверь логику парсинга/нормализации" >&2
fi
