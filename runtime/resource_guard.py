#!/usr/bin/env python3
"""Run a short, isolated development command with conservative Mac safeguards.

This is not a kernel resource limit. Sampling may miss spikes; game/GPU and
unrelated processes also consume resources. It is not a game controller.
"""

import argparse
import ctypes
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


GIB = 1024 ** 3
MIB = 1024 ** 2


def thermal_state():
    """Use Apple's public NSProcessInfo thermalState without spawning Swift."""
    ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    send_int = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    cls = objc.objc_getClass(b"NSProcessInfo")
    instance = send_object(cls, objc.sel_registerName(b"processInfo"))
    if not cls or not instance:
        raise RuntimeError("thermal state unavailable")
    state = int(send_int(instance, objc.sel_registerName(b"thermalState")))
    if state not in range(4):
        raise RuntimeError("unknown thermal state")
    return state


def battery_status():
    result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=3, check=True)
    match = re.search(r"(\d+)%", result.stdout)
    if not match:
        raise RuntimeError("battery percentage unavailable")
    return "AC Power" in result.stdout, int(match.group(1))


def swap_used():
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3, check=True)
    match = re.search(r"used\s*=\s*([0-9.]+)([KMG])", result.stdout)
    if not match:
        raise RuntimeError("swap status unavailable")
    return float(match.group(1)) * {"K": 1024, "M": MIB, "G": GIB}[match.group(2)]


def memory_free_percentage():
    result = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True, text=True, timeout=3, check=True)
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", result.stdout)
    if not match:
        raise RuntimeError("memory pressure reading unavailable")
    return int(match.group(1))


def swap_exceeded(current, baseline, maximum=1024*MIB, growth=256*MIB):
    # Swap can remain allocated after memory pressure has recovered. Bound
    # both its absolute total and growth during this exact session.
    return current > maximum or current - baseline > growth


def group_usage(pgid):
    result = subprocess.run(["/bin/ps", "-axo", "pgid=,rss=,pcpu="], capture_output=True, text=True, timeout=3, check=True)
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0].isdigit() and int(fields[0]) == pgid:
            members.append((int(fields[1]) * 1024, float(fields[2])))
    return sum(x[0] for x in members), sum(x[1] for x in members), len(members)


def terminate_group(pgid, proc=None):
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if proc is not None and proc.poll() is None:
            proc.terminate()
        else:
            return
    if proc is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()  # Reap the leader before checking for remaining members.
        try:
            _, _, members = group_usage(pgid)
        except Exception:
            members = 1
        if not members:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def main():
    def interrupted(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise RuntimeError("supervisor interrupted by signal %d" % signum)

    signal.signal(signal.SIGTERM, interrupted)
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--log", type=Path)
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--max-rss-mib", type=int, default=800)
    p.add_argument("--min-free-gib", type=float, default=10)
    p.add_argument("--max-swap-mib", type=int, default=1024)
    p.add_argument("--max-swap-growth-mib", type=int, default=256)
    p.add_argument("--max-cpu", type=float, default=160)
    p.add_argument("--max-output-mib", type=int, default=4)
    p.add_argument("--parent-pid", type=int)
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    disk_path = "/System/Volumes/Data"
    ac, initial_battery = battery_status()
    initial_swap = swap_used()
    initial = {"free_gib": round(shutil.disk_usage(disk_path).free / GIB, 2),
               "thermal_state": thermal_state(), "ac": ac,
               "battery_pct": initial_battery, "swap_mib": round(initial_swap / MIB, 1),
               "memory_free_pct": memory_free_percentage()}
    print(json.dumps({"preflight": initial}), flush=True)
    if initial["free_gib"] < a.min_free_gib or initial["thermal_state"] >= 2 or not ac or initial["swap_mib"] > a.max_swap_mib or initial["memory_free_pct"] < 20:
        print("Preflight failed: insufficient disk, elevated thermal state, no AC, or excessive swap.", file=sys.stderr)
        return 2
    if a.check:
        return 0
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command or not a.log:
        p.error("--log and a command after -- are required")
    a.log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    sample_at = started
    battery_at = started
    last_status = started
    consecutive_high_cpu = 0
    max_rss = 0
    max_cpu = 0.0
    output_bytes = 0
    reason = None
    reported = False
    proc = None
    selector = selectors.DefaultSelector()
    with a.log.open("wb") as log:
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
            assert proc.stdout is not None
            print(json.dumps({"started_pid": proc.pid}), flush=True)
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                if a.parent_pid is not None and os.getppid() != a.parent_pid:
                    reason = "session supervisor exited"
                    break
                for key, _ in selector.select(timeout=0.25):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output_bytes += len(data)
                        log.write(data[:max(0, a.max_output_mib * MIB - log.tell())])
                        log.flush()
                        if output_bytes > a.max_output_mib * MIB:
                            reason = "output limit"
                now = time.monotonic()
                if now >= sample_at:
                    rss, cpu, members = group_usage(proc.pid)
                    max_rss = max(max_rss, rss)
                    max_cpu = max(max_cpu, cpu)
                    tstate = thermal_state()
                    free = shutil.disk_usage(disk_path).free
                    used_swap = swap_used()
                    consecutive_high_cpu = consecutive_high_cpu + 1 if cpu > a.max_cpu else 0
                    if rss > a.max_rss_mib * MIB:
                        reason = "process group memory limit"
                    elif free < a.min_free_gib * GIB:
                        reason = "disk reserve"
                    elif swap_exceeded(used_swap, initial_swap, a.max_swap_mib*MIB, a.max_swap_growth_mib*MIB):
                        reason = "swap threshold"
                    elif memory_free_percentage() < 20:
                        reason = "system memory pressure threshold"
                    elif tstate >= 2:
                        reason = "serious or critical thermal state"
                    elif consecutive_high_cpu >= 3:
                        reason = "sustained process group CPU threshold"
                    if now >= battery_at:
                        on_ac, pct = battery_status()
                        if not on_ac or pct <= initial_battery - 2:
                            reason = "AC disconnected or battery dropped"
                        battery_at = now + 10
                    if now - last_status >= 10:
                        print(json.dumps({"elapsed_s": round(now-started, 1), "rss_mib": round(rss/MIB, 1), "cpu_pct": round(cpu, 1), "members": members, "thermal_state": tstate, "free_gib": round(free/GIB, 2)}), flush=True)
                        last_status = now
                    sample_at = now + 2
                if now - started > a.timeout:
                    reason = "wall-clock timeout"
                if reason:
                    break
                if proc.poll() is not None and not selector.get_map():
                    break
            return_code = proc.poll()
            if reason:
                print("Stopped: " + reason, file=sys.stderr, flush=True)
            if return_code is None:
                terminate_group(proc.pid, proc)
                return_code = proc.wait(timeout=5)
            # Also stop any descendants left in this isolated group.
            terminate_group(proc.pid, proc)
            print(json.dumps({"result": {"exit_code": return_code, "reason": reason, "duration_s": round(time.monotonic()-started, 2), "peak_sampled_rss_mib": round(max_rss/MIB, 1), "peak_sampled_cpu_pct": round(max_cpu, 1), "output_bytes": output_bytes}}), flush=True)
            reported = True
            return 124 if reason else return_code
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            selector.close()
            if proc is not None:
                terminate_group(proc.pid, proc)
            if not reported:
                print(json.dumps({"result": {"exit_code": proc.poll() if proc else None,
                    "reason": reason or "interrupted", "duration_s": round(time.monotonic()-started, 2),
                    "peak_sampled_rss_mib": round(max_rss/MIB, 1), "peak_sampled_cpu_pct": round(max_cpu, 1),
                    "output_bytes": output_bytes}}), flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Guard failed closed: " + str(exc), file=sys.stderr)
        sys.exit(2)
