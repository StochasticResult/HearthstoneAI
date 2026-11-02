#!/usr/bin/env python3
"""Queue the next app Start for a 16-request, no-game A/B evaluation.

No credential handling here. The native app obtains its existing Keychain key.
This file expires in five minutes and is consumed exactly once.
"""
import json
import argparse
import os
import time
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from evaluation_session import SUITES

parser = argparse.ArgumentParser()
parser.add_argument("--suite",choices=tuple(SUITES),default="strategy-v1")
args = parser.parse_args()
data = Path.home() / "Library/Application Support/HearthstoneAI"
data.mkdir(mode=0o700, parents=True, exist_ok=True)
os.umask(0o077)
(data / "strategy-eval-request.json").write_text(json.dumps({"suite": args.suite, "expires_at": time.time()+300}))
print("Next Practice button runs %s (%d requests maximum). Expires in 5 minutes." % (args.suite,SUITES[args.suite][2]))
