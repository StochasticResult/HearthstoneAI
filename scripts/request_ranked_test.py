#!/usr/bin/env python3
"""Select a single Standard ranked test for the next Start; no key access."""
import json
import os
import time
from pathlib import Path

data = Path.home() / "Library/Application Support/HearthstoneAI"
data.mkdir(mode=0o700, parents=True, exist_ok=True)
if (data / "strategy-eval-request.json").exists():
    raise SystemExit("A strategy evaluation is already queued")
os.umask(0o077)
(data / "game-request.json").write_text(json.dumps({"mode": "ranked", "expires_at": time.time() + 300}))
print("Next app Start runs ONE Standard ranked game with existing resource limits. Expires in 5 minutes.")
