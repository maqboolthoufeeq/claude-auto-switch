#!/bin/sh
cd "$(dirname "$0")" || exit 1
if command -v uv >/dev/null 2>&1; then
  exec uv run --offline launch.py
fi
if [ -x "$HOME/.local/bin/uv" ]; then
  exec "$HOME/.local/bin/uv" run --offline launch.py
fi
echo "Install uv and run uv sync once, then reopen Switchboard."
exit 1
