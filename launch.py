"""Open the existing local dashboard, or start it with the current uv environment."""

import argparse
import json
from pathlib import Path
import urllib.request

from platform_runtime import check_macos_version

from rotator import DATA_DIR, RotatorError, main as rotator_main
from rotator_ui import main as dashboard_main, open_dashboard


def main() -> int:
    check_macos_version()
    # The dashboard's own options; rotator_ui.main() reads the same ones (and any others) when it starts.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=DATA_DIR / "rotator.json")
    parser.add_argument("--no-browser", action="store_true")
    args, _ = parser.parse_known_args()
    config_path = args.config.expanduser().resolve()
    try:
        link = (config_path.parent / ".shared-rotator" / "dashboard-url").read_text().strip()
        if link.startswith("http://127.0.0.1:56729/#token="):
            # Reopen only a dashboard that accepts this saved token (a stale one answers 403), and open it with a
            # single-use code. Loopback only: a configured HTTP proxy must never see the token.
            request = urllib.request.Request(
                "http://127.0.0.1:56729/api/link-code", data=b"{}", method="POST",
                headers={"X-Rotator-Token": link.partition("#token=")[2], "Content-Type": "application/json"})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
                code = json.loads(response.read())["code"]
            print(f"Switchboard is already open: {link}", flush=True)
            if not args.no_browser:
                open_dashboard(code)
            return 0
    except (OSError, ValueError, KeyError):  # no saved link, nothing listening, a 403 (HTTPError), or an old dashboard
        pass
    if not config_path.exists():
        rotator_main(["--config", str(config_path), "init"])
    return dashboard_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RotatorError as exc:
        raise SystemExit(str(exc))
