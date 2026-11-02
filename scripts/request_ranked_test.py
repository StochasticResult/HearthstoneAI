#!/usr/bin/env python3
"""Select a single Standard ranked test for the next Start; no key access."""
import json
import argparse
import os
import time
from pathlib import Path

data = Path.home() / "Library/Application Support/HearthstoneAI"
parser = argparse.ArgumentParser()
parser.add_argument("--batch", "--continuous", action="store_true", help="continuous ranked play without a game-count quota")
args = parser.parse_args()
data.mkdir(mode=0o700, parents=True, exist_ok=True)
if (data / "strategy-eval-request.json").exists():
    raise SystemExit("A strategy evaluation is already queued")
os.umask(0o077)
(data / "game-request.json").write_text(json.dumps({"mode": "ranked_auto" if args.batch else "ranked", "expires_at": time.time() + 300}))
print("Queued %s Standard ranked play. The launch request expires in 5 minutes; this is not a gameplay-duration limit." % ("continuous" if args.batch else "one game of"))
