"""Open the existing local dashboard, or start it with the current uv environment."""

import urllib.request

from platform_runtime import check_macos_version

from rotator import DATA_DIR, RotatorError, main as rotator_main
from rotator_ui import main as dashboard_main, open_dashboard


def main() -> int:
    check_macos_version()
    try:
        link = (DATA_DIR / ".shared-rotator" / "dashboard-url").read_text().strip()
        if link.startswith("http://127.0.0.1:56729/#token="):
            # Reopen only a dashboard that accepts this saved token; a stale link opens a page that refuses it.
            probe = urllib.request.Request("http://127.0.0.1:56729/api/task",
                                           headers={"X-Rotator-Token": link.partition("#token=")[2]})
            urllib.request.urlopen(probe, timeout=2).close()
            open_dashboard(link)
            return 0
    except OSError:  # no saved link, nothing listening, or a 403 (HTTPError is an OSError)
        pass
    if not (DATA_DIR / "rotator.json").exists():
        rotator_main(["--config", str(DATA_DIR / "rotator.json"), "init"])
    return dashboard_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RotatorError as exc:
        raise SystemExit(str(exc))
