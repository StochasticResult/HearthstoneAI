"""Check interruption cleanup using only a disposable sleep child."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

guard = Path(__file__).resolve().parent.parent / "runtime/resource_guard.py"
with tempfile.TemporaryDirectory() as temp:
    proc = subprocess.Popen(["/usr/bin/python3", str(guard), "--timeout", "15", "--log", str(Path(temp) / "output.log"),
                             "--", "/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 15"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        preflight = json.loads(proc.stdout.readline())
        assert preflight["preflight"]["thermal_state"] < 2
        child_pid = json.loads(proc.stdout.readline())["started_pid"]
        time.sleep(.15)
        proc.terminate()
        time.sleep(.15)
        proc.terminate()
        output, _ = proc.communicate(timeout=7)
        assert '"reason": "interrupted"' in output
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("guard left its child process running")
        print("PASS repeated supervisor interruption cleans up its isolated child and reports metrics")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
