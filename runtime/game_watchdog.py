#!/usr/bin/env python3
"""Supervise a single explicitly started local game with resource safeguards.

This is a sampled watchdog, not a GPU throttle or a promise against heat spikes.
It sends no game input. On exit it closes only the exact process it claimed.
"""
import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from resource_guard import GIB, MIB, battery_status, swap_used, thermal_state, memory_free_percentage, memory_pressure_level, memory_stop_reason

GAME_EXEC = "/Applications/Hearthstone/Hearthstone.app/Contents/MacOS/Hearthstone"
LAUNCHER_EXEC = "/Applications/Hearthstone/Hearthstone Beta Launcher.app/Contents/MacOS/Hearthstone Beta Launcher"
OPTIONS = os.path.expanduser("~/Library/Preferences/Blizzard/Hearthstone/options.txt")


def change_fps(path, expected, replacement):
    """Change just the exact fps setting; refuse unexpected concurrent edits."""
    with open(path, "r", encoding="utf-8") as handle:
        contents = handle.read()
    matches = re.findall(r"(?m)^targetframerate=(\d+)[ \t]*$", contents)
    if len(matches) != 1 or (expected is not None and int(matches[0]) != expected):
        raise RuntimeError("frame rate setting missing, ambiguous or changed")
    revised = re.sub(r"(?m)^targetframerate=\d+[ \t]*$", "targetframerate=%d" % replacement, contents, count=1)
    mode = os.stat(path).st_mode & 0o777
    fd, temp_path = tempfile.mkstemp(prefix=".fps-", dir=os.path.dirname(path))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(revised)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return int(matches[0])


def processes(expected):
    result = subprocess.run(["/bin/ps", "-axo", "pid=,rss=,pcpu=,comm="], capture_output=True, text=True, timeout=3, check=True)
    found = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4 and fields[3] == expected:
            found.append((int(fields[0]), int(fields[1]) * 1024, float(fields[2])))
    return found


def stop_exact(pid, expected):
    if not any(p[0] == pid for p in processes(expected)):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(p[0] == pid for p in processes(expected)):
            return
        time.sleep(0.2)
    if any(p[0] == pid for p in processes(expected)):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", default=GAME_EXEC)
    parser.add_argument("--claim-pid", type=int, help="for disposable watchdog self-tests only")
    parser.add_argument("--startup-seconds", type=int, default=30)
    parser.add_argument("--run-seconds", type=int, default=75, help="0 monitors resources without a game-duration quota")
    parser.add_argument("--temporary-fps", type=int, choices=(30,), help="temporarily set 30 fps and restore afterwards")
    parser.add_argument("--background-policy", action="store_true", help="lower the claimed process scheduling/I/O priority; not a CPU cap")
    parser.add_argument("--parent-pid", type=int, help="stop and restore settings if the session supervisor exits")
    parser.add_argument("--swap-baseline-mib", type=float)
    args = parser.parse_args()
    if args.run_seconds < 0:
        parser.error("run seconds must be nonnegative")
    if args.swap_baseline_mib is not None and (not math.isfinite(args.swap_baseline_mib) or args.swap_baseline_mib < 0):
        parser.error("swap baseline must be finite and nonnegative")
    if args.expected != GAME_EXEC and args.claim_pid is None:
        parser.error("only the game executable may be discovered automatically")

    def interrupted(signum, _frame):
        # Native termination and the session's cleanup can both signal us.
        # A second signal must not interrupt closing the exact claimed game.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise RuntimeError("watchdog interrupted by signal %d" % signum)

    signal.signal(signal.SIGTERM, interrupted)
    initial_ac, initial_pct = battery_status()
    initial_swap = swap_used()
    swap_baseline = initial_swap if args.swap_baseline_mib is None else args.swap_baseline_mib * MIB
    free_memory_pct = memory_free_percentage()
    previous_pressure = memory_pressure_level()
    memory_reason = memory_stop_reason(initial_swap, swap_baseline, previous_pressure, free_memory_pct)
    if memory_reason:
        raise RuntimeError("preflight safety check failed: " + memory_reason)
    if not initial_ac or thermal_state() >= 1 or shutil.disk_usage("/System/Volumes/Data").free < 10 * GIB:
        raise RuntimeError("preflight safety check failed")
    if args.claim_pid is None and processes(args.expected):
        raise RuntimeError("game already running; refusing to claim it")
    original_launchers = {p[0] for p in processes(LAUNCHER_EXEC)} if args.expected == GAME_EXEC else set()
    if args.temporary_fps and args.expected != GAME_EXEC:
        parser.error("temporary frame rate applies only to the game")
    previous_fps = change_fps(OPTIONS, None, args.temporary_fps) if args.temporary_fps else None
    print(json.dumps({"watchdog": "armed", "pid": os.getpid(), "max_game_seconds": args.run_seconds}), flush=True)
    claimed = None
    start = time.monotonic()
    game_start = None
    previous_status = start
    last_memory_check = start
    high_cpu_since = None
    max_rss = 0
    max_cpu = 0.0
    reason = None
    try:
        while True:
            now = time.monotonic()
            if args.parent_pid is not None and os.getppid() != args.parent_pid:
                reason = "session supervisor exited"
                break
            active = processes(args.expected)
            if claimed is None:
                matches = [p for p in active if args.claim_pid is None or p[0] == args.claim_pid]
                if len(matches) > 1:
                    reason = "ambiguous game processes"
                    break
                if matches:
                    claimed = matches[0][0]
                    game_start = now
                    if args.background_policy:
                        subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(claimed)], timeout=3, check=True, capture_output=True)
                        print(json.dumps({"background_policy": "applied"}), flush=True)
                    print(json.dumps({"claimed_pid": claimed}), flush=True)
                elif now - start >= args.startup_seconds:
                    reason = "game did not start"
                    break
            else:
                current = next((p for p in active if p[0] == claimed), None)
                if current is None:
                    reason = "game exited"
                    break
                _, rss, cpu = current
                max_rss = max(max_rss, rss); max_cpu = max(max_cpu, cpu)
                # %CPU counts cores. Startup routinely uses 3-4 cores even
                # in successfully completed games; that is not overheating.
                # Sustained load is recorded for boundary maintenance. Actual
                # heat, process RAM and power remain independent safeguards.
                high_cpu_since = (high_cpu_since or now) if cpu > 250 and now-game_start >= 60 else None
                ac, pct = battery_status()
                if now - last_memory_check >= 5:
                    free_memory_pct = memory_free_percentage()
                    last_memory_check = now
                current_swap = swap_used()
                pressure = memory_pressure_level()
                memory_reason = memory_stop_reason(current_swap, swap_baseline, pressure, free_memory_pct,
                                                   defer_maintenance=True)
                maintenance_reason = memory_stop_reason(current_swap, swap_baseline, pressure, free_memory_pct)
                tstate = thermal_state()
                free_disk = shutil.disk_usage('/System/Volumes/Data').free
                if not maintenance_reason and tstate == 1:
                    maintenance_reason = 'fair thermal state'
                if not maintenance_reason and high_cpu_since and now-high_cpu_since >= 60:
                    maintenance_reason = 'sustained game CPU load'
                if not maintenance_reason and free_disk < 10*GIB:
                    maintenance_reason = 'disk reserve'
                if pressure != previous_pressure:
                    print(json.dumps({'memory_pressure_level': pressure, 'pressure_transition': True,
                                      'swap_mib': round(current_swap/MIB,1)}), flush=True)
                    previous_pressure = pressure
                if tstate >= 2:
                    reason = "serious or critical thermal state"
                elif not ac or pct <= initial_pct - 1:
                    reason = "power or battery threshold"
                elif rss > 1800 * MIB:
                    reason = "game memory threshold"
                elif memory_reason:
                    reason = memory_reason
                elif free_disk < 2 * GIB:
                    reason = "disk reserve"
                elif args.run_seconds and now - game_start >= args.run_seconds:
                    reason = "short test deadline"
                if now - previous_status >= 10:
                    print(json.dumps({"game_seconds": round(now-game_start, 1), "rss_mib": round(rss/MIB, 1), "cpu_pct": round(cpu, 1), "thermal_state": tstate, "memory_free_pct": free_memory_pct,
                                      "swap_mib": round(current_swap/MIB,1), "initial_swap_mib": round(initial_swap/MIB,1),
                                      "swap_baseline_mib": round(swap_baseline/MIB,1), "memory_pressure_level": pressure,
                                      "maintenance_pending": maintenance_reason, 'free_gib': round(free_disk/GIB,2)}), flush=True)
                    previous_status = now
                if reason:
                    break
            time.sleep(1)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            # A parent can exit in the short interval after launch but before
            # the first sampling tick. Preflight proved no game was running;
            # reclaim exactly one new game here so it cannot be orphaned.
            if claimed is None and args.expected == GAME_EXEC:
                final_matches = processes(args.expected)
                if len(final_matches) == 1:
                    claimed = final_matches[0][0]
            if claimed is not None:
                stop_exact(claimed, args.expected)
            if args.expected == GAME_EXEC:
                for pid, _, _ in processes(LAUNCHER_EXEC):
                    if pid not in original_launchers:
                        stop_exact(pid, LAUNCHER_EXEC)
        finally:
            if previous_fps is not None:
                try:
                    change_fps(OPTIONS, args.temporary_fps, previous_fps)
                    print(json.dumps({"restored_target_fps": previous_fps}), flush=True)
                except Exception:
                    print("frame rate restore failed; manual check required", file=sys.stderr, flush=True)
        print(json.dumps({"watchdog_result": reason or "interrupted", "peak_sampled_rss_mib": round(max_rss/MIB, 1), "peak_sampled_cpu_pct": round(max_cpu, 1)}), flush=True)
    return 0 if reason in ("game exited", "short test deadline") else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("watchdog failed closed: " + str(error), file=sys.stderr, flush=True)
        sys.exit(2)
