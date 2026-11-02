#!/usr/bin/env python3
"""Queue the next app Start for a 16-request, no-game A/B evaluation.

No credential handling here. The native app obtains its existing Keychain key.
This file expires in five minutes and is consumed exactly once.
"""
import json
import os
import time
from pathlib import Path

data = Path.home() / "Library/Application Support/HearthstoneAI"
data.mkdir(mode=0o700, parents=True, exist_ok=True)
os.umask(0o077)
(data / "strategy-eval-request.json").write_text(json.dumps({"suite": "strategy-v1", "expires_at": time.time()+300}))
print("Next app Start runs strategy evaluation only (16 API requests maximum; no game input). Expires in 5 minutes.")
