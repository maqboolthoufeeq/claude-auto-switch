#!/bin/zsh
cd "${0:A:h}" || exit 1

if command -v uv >/dev/null 2>&1; then
  exec uv run --offline launch.py
fi

for uv_path in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [[ -x "$uv_path" ]]; then
    exec "$uv_path" run --offline launch.py
  fi
done

echo "uv was not found. Install uv, then open Switchboard again."
read -r "?Press Enter to close this window."
exit 1
