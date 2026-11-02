#!/usr/bin/env python3
"""Update only this app's local Python/helper runtime, preserving native identity."""
import os
from pathlib import Path
import shutil
import subprocess
import hashlib
import json
import plistlib

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
    digest=hashlib.sha256()
    for path in sorted(p for p in staging.rglob('*') if p.is_file() and p.name!='build-info.json'):
        digest.update(str(path.relative_to(staging)).encode()+b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    app=Path.home()/'Applications/Hearthstone AI.app/Contents'
    native=app/'MacOS/HearthstoneAI'
    if native.is_file():digest.update(hashlib.sha256(native.read_bytes()).digest())
    native_version=plistlib.loads((app/'Info.plist').read_bytes()).get('CFBundleShortVersionString','unknown')
    revision=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    manifest={'version_id':str(native_version)+'-'+digest.hexdigest()[:12],
              'content_sha256':digest.hexdigest(),'git_revision':revision,'native_version':native_version}
    (staging/'build-info.json').write_text(json.dumps(manifest)+'\n')
    if target.exists(): target.rename(backup)
    staging.rename(target)
    if backup.exists(): shutil.rmtree(backup)
    print("Updated HearthstoneAI runtime without changing the native app signature")
    print('Runtime version:',manifest['version_id'])


if __name__ == "__main__":
    main()
