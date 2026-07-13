#!/usr/bin/env bash
# Convenience launcher for local runs (not the systemd path). Sources ~/.katana-bot/env if
# present, then runs the executor. Defaults to a single DRY_RUN pass — safe.
#   ./bot/run.sh once      # one DRY_RUN pass (default)
#   ./bot/run.sh loop      # continuous loop (respects DRY_RUN from env)
#   ./bot/run.sh reset     # clear kill-switch / dedup
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=${KT_ENV:-$HOME/.katana-bot/env}
[ -f "$ENV" ] && set -a && . "$ENV" && set +a
exec python3 -m bot.executor "${1:-once}"
