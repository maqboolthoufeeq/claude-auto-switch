import sys
from pathlib import Path


def write_executable(path: Path, source: str) -> Path:
    if sys.platform == "win32":
        from distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(path.parent))
        maker.executable = sys.executable
        maker.variants = {""}
        maker.script_template = source.replace("%", "%%")
        maker.make(f"{path.name} = fixture:main")
        return path.with_suffix(".exe")
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path
