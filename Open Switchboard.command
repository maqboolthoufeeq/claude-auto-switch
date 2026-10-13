#!/bin/zsh
cd "${0:A:h}" || exit 1

# Reopen the dashboard already serving the one fixed local address.
dashboard_link=.shared-rotator/dashboard-url
if [[ -r "$dashboard_link" ]] &&
   /usr/bin/curl --silent --fail --max-time 2 http://127.0.0.1:56729/ | /usr/bin/grep -q '<title>Switchboard' ; then
  dashboard_url="$(<"$dashboard_link")"
  if [[ "$dashboard_url" == http://127.0.0.1:56729/#token=* ]]; then
    /usr/bin/open "$dashboard_url"
    exit 0
  fi
fi

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
