"""Watchdog deadline test against a disposable child, not the game."""
import subprocess
import tempfile
import time
import json
import os
import signal
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

# Exercise both ranked release paths with disposable processes. The claimed
# process must survive the guard with its original PID in each case.
for departure in ('released to user', 'native application exited'):
    child = subprocess.Popen(['/bin/sleep', '25'])
    owner = subprocess.Popen(['/bin/sleep', '25'])
    guard_proc = None
    try:
        guard_proc = subprocess.Popen(['/usr/bin/python3', str(guard), '--expected', '/bin/sleep',
            '--claim-pid', str(child.pid), '--startup-seconds', '3', '--run-seconds', '0',
            '--parent-pid', str(os.getpid()), '--native-owner-pid', str(owner.pid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert json.loads(guard_proc.stdout.readline())['watchdog'] == 'armed'
        assert json.loads(guard_proc.stdout.readline())['claimed_pid'] == child.pid
        if departure == 'released to user':
            guard_proc.send_signal(signal.SIGUSR2)
        else:
            owner.terminate()
            owner.wait(timeout=3)
        out, err = guard_proc.communicate(timeout=8)
        assert guard_proc.returncode == 0, err
        assert json.loads(out.strip().splitlines()[-1])['watchdog_result'] == departure
        assert child.poll() is None
        print('PASS retained game survives ' + departure)
    finally:
        for proc in (guard_proc, owner, child):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

# A stubborn disposable child keeps cleanup in progress when a second stop
# arrives, reproducing the native-app/session double-termination race.
child = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 15"])
guard_proc = None
try:
    guard_proc = subprocess.Popen(["/usr/bin/python3", str(guard), "--expected", "/bin/sleep",
        "--claim-pid", str(child.pid), "--startup-seconds", "3", "--run-seconds", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert json.loads(guard_proc.stdout.readline())["watchdog"] == "armed"
    assert json.loads(guard_proc.stdout.readline())["claimed_pid"] == child.pid
    guard_proc.terminate()
    time.sleep(.15)
    guard_proc.terminate()
    output, _ = guard_proc.communicate(timeout=7)
    child.wait(timeout=2)
    assert child.returncode != 0
    assert '"watchdog_result": "interrupted"' in output
    print("PASS repeated stop cannot interrupt claimed-child cleanup")
finally:
    if guard_proc and guard_proc.poll() is None:
        guard_proc.kill(); guard_proc.wait(timeout=2)
    if child.poll() is None:
        child.kill(); child.wait(timeout=2)
