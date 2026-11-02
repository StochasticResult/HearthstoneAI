#!/usr/bin/env python3
"""Update only this app's local Python/helper runtime, preserving native identity."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
DATA = Path.home() / "Library/Application Support/HearthstoneAI"


def main():
    ps = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True, check=True).stdout
    if any(("HearthstoneAI/runtime/session.py" in line or "Hearthstone AI.app/Contents/Resources/runtime/session.py" in line)
           for line in ps.splitlines() if not line.startswith("/bin/zsh")):
        raise SystemExit("Stop the running game session before updating its runtime")
    DATA.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    staging = DATA / "runtime-staging"
    backup = DATA / "runtime-previous"
    target = DATA / "runtime"
    for path in (staging, backup):
        if path.exists(): shutil.rmtree(path)
    shutil.copytree(ROOT / "runtime", staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if target.exists(): target.rename(backup)
    staging.rename(target)
    if backup.exists(): shutil.rmtree(backup)
    print("Updated HearthstoneAI runtime without changing the native app signature")


if __name__ == "__main__":
    main()
