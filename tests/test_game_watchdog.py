"""Watchdog deadline test against a disposable child, not the game."""
import subprocess
import tempfile
from pathlib import Path
from game_watchdog import change_fps, memory_free_percentage

with tempfile.TemporaryDirectory() as directory:
    options = Path(directory) / "options.txt"
    original = "quality=0\ntargetframerate=60\nnext=1\n"
    options.write_text(original)
    assert change_fps(str(options), 60, 30) == 60
    assert options.read_text() == "quality=0\ntargetframerate=30\nnext=1\n"
    assert change_fps(str(options), 30, 60) == 30
    assert options.read_text() == original
    try:
        change_fps(str(options), 30, 60)
        raise AssertionError("changed setting accepted")
    except RuntimeError:
        pass
assert 0 <= memory_free_percentage() <= 100
print("PASS temporary fps restore and memory pressure reader")

child = subprocess.Popen(["/bin/sleep", "10"])
try:
    guard = Path(__file__).resolve().parent.parent / "runtime/game_watchdog.py"
    result = subprocess.run(["/usr/bin/python3", str(guard), "--expected", "/bin/sleep", "--claim-pid", str(child.pid),
                             "--startup-seconds", "3", "--run-seconds", "1", "--background-policy"], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    child.wait(timeout=3)
    assert child.returncode != 0
    assert '"background_policy": "applied"' in result.stdout
    print("PASS game watchdog deadline closes only the claimed test child")
finally:
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=3)
