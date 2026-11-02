import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ParentCleanupTests(unittest.TestCase):
    def test_guard_parent_mismatch_reaps_owned_child(self):
        guard = Path(__file__).resolve().parent.parent / "runtime/resource_guard.py"
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, str(guard), "--parent-pid", "0", "--timeout", "8",
                "--log", str(Path(tmp)/"test.log"), "--", "/bin/sleep", "10"],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 124, run.stderr)
            events = [json.loads(line) for line in run.stdout.splitlines()]
            child = next(event["started_pid"] for event in events if "started_pid" in event)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertEqual(events[-1]["result"]["reason"], "session supervisor exited")


if __name__ == "__main__":
    unittest.main()
