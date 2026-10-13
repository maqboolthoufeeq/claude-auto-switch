"""Open the existing local dashboard, or start it with the current uv environment."""

import urllib.request
import webbrowser

from rotator import BASE_DIR, RotatorError, main as rotator_main
from rotator_ui import main as dashboard_main


def main() -> int:
    try:
        link = (BASE_DIR / ".shared-rotator" / "dashboard-url").read_text().strip()
        if link.startswith("http://127.0.0.1:56729/#token="):
            with urllib.request.urlopen("http://127.0.0.1:56729/", timeout=2) as response:
                if b"<title>Switchboard" in response.read(64000):
                    webbrowser.open(link)
                    return 0
    except OSError:
        pass
    if not (BASE_DIR / "rotator.json").exists():
        rotator_main(["--config", str(BASE_DIR / "rotator.json"), "init"])
    return dashboard_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RotatorError as exc:
        raise SystemExit(str(exc))
