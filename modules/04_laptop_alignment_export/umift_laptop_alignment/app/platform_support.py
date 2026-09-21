"""Small desktop platform adapters used by the browser backend."""
import os
import subprocess
import sys
from pathlib import Path


def open_directory(path: str | Path) -> None:
    target = str(Path(path).expanduser().resolve())
    if sys.platform == "win32":
        os.startfile(target)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", target], check=True)
