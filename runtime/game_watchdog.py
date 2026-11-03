#!/usr/bin/env python3
"""Supervise a single explicitly started local game with resource safeguards.

This is a sampled watchdog, not a GPU throttle or a promise against heat spikes.
Ranked play hands its client back on normal stops. Only the claimed game
can be closed for a resource fault or a disposable session.
"""
import argparse
import datetime
import fcntl
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from resource_guard import GIB, MIB, battery_status, swap_used, thermal_state, memory_free_percentage, memory_pressure_level, memory_stop_reason

GAME_EXEC = "/Applications/Hearthstone/Hearthstone.app/Contents/MacOS/Hearthstone"
LAUNCHER_EXEC = "/Applications/Hearthstone/Hearthstone Beta Launcher.app/Contents/MacOS/Hearthstone Beta Launcher"
OPTIONS = os.path.expanduser("~/Library/Preferences/Blizzard/Hearthstone/options.txt")
GAME_RSS_LIMIT_MIB = 1800
GAME_RSS_BOUNDARY_MIB = GAME_RSS_LIMIT_MIB * 95 // 100
BOUNDARY_RECYCLE = 'postgame memory recycle'
RECOVERABLE_GAME_STOPS = {BOUNDARY_RECYCLE, 'game memory threshold', 'critical system memory pressure',
    'system memory availability threshold', 'unknown system memory status', 'serious or critical thermal state',
    'power or battery threshold', 'disk reserve'}


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


def process_started(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        info = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                              text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
        return int(datetime.datetime.strptime(info.stdout.strip(), '%a %b %d %H:%M:%S %Y').timestamp())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", default=GAME_EXEC)
    parser.add_argument("--claim-pid", type=int, help="for disposable watchdog self-tests only")
    parser.add_argument("--startup-seconds", type=int, default=30)
    parser.add_argument("--run-seconds", type=int, default=75, help="0 monitors resources without a game-duration quota")
    parser.add_argument("--temporary-fps", type=int, choices=(30,), help="temporarily set 30 fps and restore afterwards")
    parser.add_argument("--background-policy", action="store_true", help="lower the claimed process scheduling/I/O priority; not a CPU cap")
    parser.add_argument("--parent-pid", type=int, help="stop and restore settings if the session supervisor exits")
    parser.add_argument("--native-owner-pid", type=int, help="in ranked play, retain resource protection if the AI supervisor disappears")
    parser.add_argument("--adopt-pid",type=int)
    parser.add_argument("--adopt-start",type=int)
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
    preflight_reason = memory_stop_reason(initial_swap, swap_baseline, previous_pressure, free_memory_pct, growth=None)
    if not initial_ac:
        preflight_reason = 'waiting for AC power'
    elif thermal_state() >= 1:
        preflight_reason = 'waiting for nominal thermal state'
    elif shutil.disk_usage("/System/Volumes/Data").free < 10 * GIB:
        preflight_reason = 'disk reserve'
    if preflight_reason:
        if args.native_owner_pid:
            print(json.dumps({'watchdog':'waiting_for_resources','reason':preflight_reason}), flush=True)
            return 2
        raise RuntimeError("preflight safety check failed: " + preflight_reason)
    if args.native_owner_pid and args.expected == GAME_EXEC:
        guard_lock = (Path.home()/'Library/Application Support/HearthstoneAI/game-guard.lock').open('a+')
        fcntl.flock(guard_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    existing=processes(args.expected)
    if args.adopt_pid is not None:
        if (not args.native_owner_pid or args.expected!=GAME_EXEC or args.claim_pid is not None or not args.adopt_start
                or [p[0] for p in existing]!=[args.adopt_pid] or process_started(args.adopt_pid)!=args.adopt_start):
            raise RuntimeError('retained Hearthstone identity changed before adoption')
    elif args.claim_pid is None and existing:
        raise RuntimeError("game already running; refusing to claim it")
    original_launchers = {p[0] for p in processes(LAUNCHER_EXEC)} if args.expected == GAME_EXEC else set()
    if args.temporary_fps and args.expected != GAME_EXEC:
        parser.error("temporary frame rate applies only to the game")
    owner_exit = None
    detached = False
    release_requested = False
    recycle_requested = False
    def release(_signum,_frame):
        nonlocal release_requested
        release_requested = True
    def recycle(_signum,_frame):
        nonlocal recycle_requested
        recycle_requested = True
    def detach(_signum=None, _frame=None):
        nonlocal detached
        detached = True

    def report(record):
        if detached:
            return
        try:
            print(json.dumps(record), flush=True)
        except BrokenPipeError:
            if owner_exit is None:
                raise
            detach()
            with open(os.devnull, 'w') as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())

    if args.native_owner_pid is not None:
        if not args.parent_pid or args.native_owner_pid <= 1:
            parser.error("ranked game guard requires both native and supervisor identities")
        owner_exit = select.kqueue()
        owner_exit.control([select.kevent(args.native_owner_pid, filter=select.KQ_FILTER_PROC,
                           flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)],0,0)
        signal.signal(signal.SIGUSR1, detach)
        signal.signal(signal.SIGUSR2, release)
        signal.signal(signal.SIGHUP, recycle)
    previous_fps = change_fps(OPTIONS, None, args.temporary_fps) if args.temporary_fps else None
    report({"watchdog": "armed", "pid": os.getpid(), "max_game_seconds": args.run_seconds})
    claimed = None
    claimed_start = None
    recycle_reported = False
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
            if owner_exit is not None and owner_exit.control(None,1,0):
                reason = "native application exited"
                break
            if args.parent_pid is not None and os.getppid() != args.parent_pid:
                if owner_exit is None:
                    reason = "session supervisor exited"
                    break
                detach()
            active = processes(args.expected)
            if claimed is None:
                selected = args.adopt_pid or args.claim_pid
                matches = [p for p in active if selected is None or p[0]==selected]
                if args.adopt_pid and (not matches or process_started(args.adopt_pid)!=args.adopt_start):
                    reason='retained game identity changed'
                    break
                if detached and not matches:
                    reason = "supervisor exited before game launch"
                    break
                if len(matches) > 1:
                    reason = "ambiguous game processes"
                    break
                if matches:
                    claimed = matches[0][0]
                    if args.native_owner_pid:
                        claimed_start = process_started(claimed)
                    game_start = now
                    if args.background_policy:
                        subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(claimed)], timeout=3, check=True, capture_output=True)
                        report({"background_policy": "applied"})
                    report({"claimed_pid": claimed})
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
                maintenance_reason = memory_stop_reason(current_swap, swap_baseline, pressure, free_memory_pct, growth=None)
                tstate = thermal_state()
                free_disk = shutil.disk_usage('/System/Volumes/Data').free
                if not maintenance_reason and tstate == 1:
                    maintenance_reason = 'fair thermal state'
                if not maintenance_reason and high_cpu_since and now-high_cpu_since >= 60:
                    maintenance_reason = 'sustained game CPU load'
                if not maintenance_reason and free_disk < 10*GIB:
                    maintenance_reason = 'disk reserve'
                if args.native_owner_pid and rss > GAME_RSS_LIMIT_MIB*MIB and not recycle_reported:
                    report({'recycle_after_game':True, 'game_pid':claimed, 'rss_mib':round(rss/MIB,1)})
                    recycle_reported = True
                if pressure != previous_pressure:
                    report({'memory_pressure_level': pressure, 'pressure_transition': True,
                            'swap_mib': round(current_swap/MIB,1)})
                    previous_pressure = pressure
                if tstate >= 2:
                    reason = "serious or critical thermal state"
                elif not ac or pct <= initial_pct - 1:
                    reason = "power or battery threshold"
                elif not args.native_owner_pid and rss > GAME_RSS_LIMIT_MIB*MIB:
                    reason = "game memory threshold"
                elif memory_reason:
                    reason = memory_reason
                elif free_disk < 2 * GIB:
                    reason = "disk reserve"
                elif args.run_seconds and now - game_start >= args.run_seconds:
                    reason = "short test deadline"
                if now - previous_status >= 10:
                    report({"game_seconds": round(now-game_start, 1), "rss_mib": round(rss/MIB, 1), "cpu_pct": round(cpu, 1), "thermal_state": tstate, "memory_free_pct": free_memory_pct,
                                      "swap_mib": round(current_swap/MIB,1), "initial_swap_mib": round(initial_swap/MIB,1),
                                      "swap_baseline_mib": round(swap_baseline/MIB,1), "memory_pressure_level": pressure,
                                      "maintenance_pending": maintenance_reason, 'free_gib': round(free_disk/GIB,2)})
                    previous_status = now
                if reason:
                    break
            if release_requested:
                reason='released to user'
                break
            if recycle_requested:
                reason = (BOUNDARY_RECYCLE if claimed_start and process_started(claimed)==claimed_start
                          else 'retained game identity changed')
                break
            time.sleep(1)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            # A parent can exit in the short interval after launch but before
            # the first sampling tick. Preflight proved no game was running;
            # reclaim exactly one new game here so it cannot be orphaned.
            preserve = bool(args.native_owner_pid and reason in ('released to user','native application exited','retained game identity changed'))
            if claimed is None and args.expected == GAME_EXEC and not detached and not preserve:
                final_matches = processes(args.expected)
                if len(final_matches) == 1:
                    claimed = final_matches[0][0]
            if claimed is not None and not preserve:
                stop_exact(claimed, args.expected)
            if args.expected == GAME_EXEC and not preserve:
                for pid, _, _ in processes(LAUNCHER_EXEC):
                    if pid not in original_launchers:
                        stop_exact(pid, LAUNCHER_EXEC)
        finally:
            if previous_fps is not None:
                try:
                    change_fps(OPTIONS, args.temporary_fps, previous_fps)
                    report({"restored_target_fps": previous_fps})
                except Exception:
                    print("frame rate restore failed; manual check required", file=sys.stderr, flush=True)
        report({"watchdog_result": reason or "interrupted", "peak_sampled_rss_mib": round(max_rss/MIB, 1), "peak_sampled_cpu_pct": round(max_cpu, 1)})
        if owner_exit is not None:
            owner_exit.close()
    return 0 if reason in ("game exited", "short test deadline", "released to user", "native application exited", BOUNDARY_RECYCLE) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("watchdog failed closed: " + str(error), file=sys.stderr, flush=True)
        sys.exit(2)
