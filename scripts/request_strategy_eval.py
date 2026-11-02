#!/usr/bin/env python3
"""Queue the next app Start for a 16-request, no-game A/B evaluation.

No credential handling here. The native app obtains its existing Keychain key.
This file expires in five minutes and is consumed exactly once.
"""
import json
import argparse
import os
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--suite",choices=("strategy-v1","model-comparison-v1","hard-comparison-v1","rule-transfer-v1","tracking-v1"),default="strategy-v1")
args = parser.parse_args()
data = Path.home() / "Library/Application Support/HearthstoneAI"
data.mkdir(mode=0o700, parents=True, exist_ok=True)
os.umask(0o077)
(data / "strategy-eval-request.json").write_text(json.dumps({"suite": args.suite, "expires_at": time.time()+300}))
print("Next app Start runs %s only (%d API requests maximum; no game input). Expires in 5 minutes." % (args.suite,4 if args.suite == 'tracking-v1' else 16 if args.suite in ("strategy-v1","rule-transfer-v1") else 48))
