import sys
from pathlib import Path


def write_executable(path: Path, source: str) -> Path:
    if sys.platform == "win32":
        script = path.with_suffix(".py")
        script.write_text(source, encoding="utf-8")
        command = path.with_suffix(".cmd")
        command.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n')
        return command
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path
