"""Open the existing local dashboard, or start it with the current uv environment."""

import json
import urllib.request

from platform_runtime import check_macos_version

from rotator import DATA_DIR, RotatorError, main as rotator_main
from rotator_ui import main as dashboard_main, open_dashboard


def main() -> int:
    check_macos_version()
    try:
        link = (DATA_DIR / ".shared-rotator" / "dashboard-url").read_text().strip()
        if link.startswith("http://127.0.0.1:56729/#token="):
            # Reopen only a dashboard that accepts this saved token (a stale one answers 403), and open it with a
            # single-use code. Loopback only: a configured HTTP proxy must never see the token.
            request = urllib.request.Request(
                "http://127.0.0.1:56729/api/link-code", data=b"{}", method="POST",
                headers={"X-Rotator-Token": link.partition("#token=")[2], "Content-Type": "application/json"})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
                code = json.loads(response.read())["code"]
            print(f"Switchboard is already open: {link}", flush=True)
            open_dashboard(code)
            return 0
    except (OSError, ValueError, KeyError):  # no saved link, nothing listening, a 403 (HTTPError), or an old dashboard
        pass
    if not (DATA_DIR / "rotator.json").exists():
        rotator_main(["--config", str(DATA_DIR / "rotator.json"), "init"])
    return dashboard_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RotatorError as exc:
        raise SystemExit(str(exc))
