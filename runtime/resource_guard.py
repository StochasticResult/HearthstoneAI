#!/usr/bin/env python3
"""Run a short, isolated development command with conservative Mac safeguards.

This is not a kernel resource limit. Sampling may miss spikes; game/GPU and
unrelated processes also consume resources. It is not a game controller.
"""

import argparse
import ctypes
import json
import math
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


def memory_pressure_level():
    """Read macOS dispatch pressure flags, not XNU's internal enum values."""
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                            capture_output=True, text=True, timeout=3, check=True)
    level = int(result.stdout.strip())
    if level not in (1, 2, 4):
        raise RuntimeError("unknown system memory pressure")
    return level  # DISPATCH_MEMORYPRESSURE_NORMAL, WARN, CRITICAL


def swap_exceeded(current, baseline, maximum=None, growth=256*MIB):
    # Historical swap is not current pressure. Keep the growth bound; an
    # explicit development CLI absolute limit is still respected if provided.
    return (maximum is not None and current > maximum) or current - baseline > growth


def memory_stop_reason(current, baseline, pressure, free_pct, maximum=None, growth=256*MIB, *, defer_maintenance=False):
    if type(pressure) is not int or pressure not in (1, 2, 4) or type(free_pct) not in (int, float) or not 0 <= free_pct <= 100:
        return "unknown system memory status"
    if pressure == 4:
        return "critical system memory pressure"
    if free_pct < (10 if defer_maintenance else 20):
        return "system memory availability threshold"
    if growth is not None and current - baseline > growth and not defer_maintenance:
        return "swap growth threshold"
    if maximum is not None and current > maximum:
        return "explicit swap total threshold"
    # A warning asks clients to reduce caches; it is not a critical state.
    # During play, ordinary availability/swap maintenance waits for game end.
    # Critical pressure, very low availability and explicit development caps
    # are still emergencies. This does not reset the session growth baseline.
    return None


def disk_stop_reason(free, reserve, *, defer_maintenance=False):
    return "disk reserve" if free < (min(reserve, 2*GIB) if defer_maintenance else reserve) else None


class DiagnosticOutput:
    """Keep a bounded prefix and recent tail, while always draining the child.

    The prefix retains opening/mulligan evidence. The tail retains terminal
    results even in a very long game. Neither file feeds the live game parser.
    """
    def __init__(self, path, limit):
        self.path = path
        self.limit = limit
        self.tail_limit = min(64*1024, limit//4)
        self.prefix_limit = limit - self.tail_limit
        self.handle = path.open('wb')
        self.tail = b''
        self.total = 0

    def write(self, data):
        self.total += len(data)
        remaining = max(0, self.prefix_limit - self.handle.tell())
        self.handle.write(data[:remaining]); self.handle.flush()
        overflow = data[remaining:]
        if overflow:
            self.tail = (self.tail + overflow)[-self.tail_limit:]
            # Partial leading lines are ignored by the review JSON reader.
            tail_path = self.path.with_name(self.path.stem + '-tail' + self.path.suffix)
            with tail_path.open('wb') as stream:
                stream.write(self.tail)

    def close(self):
        self.handle.close()


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
    p.add_argument("--timeout", type=float, default=180, help="0 disables the wall-clock quota; resource checks remain active")
    p.add_argument("--max-rss-mib", type=int, default=800)
    p.add_argument("--min-free-gib", type=float, default=10)
    p.add_argument("--max-swap-mib", type=int, help="optional explicit total cap; normal operation uses pressure and growth")
    p.add_argument("--max-swap-growth-mib", type=int,
                   help="explicit session growth limit; development commands default to 256 MiB")
    p.add_argument("--swap-baseline-mib", type=float, help="shared baseline across all matches in one autoplay session")
    p.add_argument("--max-cpu", type=float, default=160)
    p.add_argument("--max-output-mib", type=int, default=4)
    p.add_argument("--parent-pid", type=int)
    p.add_argument("--defer-maintenance", action="store_true", help="gameplay only: defer ordinary storage/swap maintenance until game end")
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.timeout < 0:
        p.error("timeout must be nonnegative")
    if a.max_output_mib < 1:
        p.error("output limit must be positive")
    if a.swap_baseline_mib is not None and (not math.isfinite(a.swap_baseline_mib) or a.swap_baseline_mib < 0):
        p.error("swap baseline must be finite and nonnegative")
    if ((a.max_swap_growth_mib is not None and a.max_swap_growth_mib < 0)
            or (a.max_swap_mib is not None and a.max_swap_mib < 0)):
        p.error("swap limits must be nonnegative")
    disk_path = "/System/Volumes/Data"
    ac, initial_battery = battery_status()
    initial_swap = swap_used()
    swap_baseline = initial_swap if a.swap_baseline_mib is None else a.swap_baseline_mib * MIB
    maximum_swap = None if a.max_swap_mib is None else a.max_swap_mib * MIB
    growth_limit_mib = a.max_swap_growth_mib
    if growth_limit_mib is None and not a.defer_maintenance:
        growth_limit_mib = 256
    growth_limit = None if growth_limit_mib is None else growth_limit_mib * MIB
    initial = {"free_gib": round(shutil.disk_usage(disk_path).free / GIB, 2),
               "thermal_state": thermal_state(), "ac": ac,
               "battery_pct": initial_battery, "swap_mib": round(initial_swap / MIB, 1),
               "memory_free_pct": memory_free_percentage(), "memory_pressure_level": memory_pressure_level(),
               "swap_baseline_mib": round(swap_baseline / MIB, 1)}
    print(json.dumps({"preflight": initial}), flush=True)
    memory_reason = memory_stop_reason(initial_swap, swap_baseline, initial['memory_pressure_level'],
                                      initial['memory_free_pct'], maximum_swap, growth_limit)
    if initial["free_gib"] < a.min_free_gib or initial["thermal_state"] >= 2 or not ac or memory_reason:
        print("Preflight failed: " + (memory_reason or "insufficient disk, elevated thermal state, or no AC"), file=sys.stderr)
        return 2
    if a.check:
        return 0
    previous_pressure = initial['memory_pressure_level']
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
    log = DiagnosticOutput(a.log, a.max_output_mib * MIB)
    try:
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
                        log.write(data)
                        if output_bytes > a.max_output_mib * MIB and not a.defer_maintenance:
                            reason = "output limit"
                now = time.monotonic()
                if now >= sample_at:
                    rss, cpu, members = group_usage(proc.pid)
                    max_rss = max(max_rss, rss)
                    max_cpu = max(max_cpu, cpu)
                    tstate = thermal_state()
                    free = shutil.disk_usage(disk_path).free
                    used_swap = swap_used()
                    pressure = memory_pressure_level()
                    free_pct = memory_free_percentage()
                    memory_reason = memory_stop_reason(used_swap, swap_baseline, pressure, free_pct,
                                                       maximum_swap, growth_limit,
                                                       defer_maintenance=a.defer_maintenance)
                    maintenance_reason = memory_stop_reason(used_swap, swap_baseline, pressure, free_pct,
                                                            maximum_swap, growth_limit)
                    if pressure != previous_pressure:
                        print(json.dumps({'memory_pressure_level': pressure, 'pressure_transition': True,
                                          'swap_mib': round(used_swap/MIB,1)}), flush=True)
                        previous_pressure = pressure
                    consecutive_high_cpu = consecutive_high_cpu + 1 if cpu > a.max_cpu else 0
                    if rss > a.max_rss_mib * MIB:
                        reason = "process group memory limit"
                    elif disk_stop_reason(free, a.min_free_gib * GIB, defer_maintenance=a.defer_maintenance):
                        reason = "disk reserve"
                    elif memory_reason:
                        reason = memory_reason
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
                        print(json.dumps({"elapsed_s": round(now-started, 1), "rss_mib": round(rss/MIB, 1), "cpu_pct": round(cpu, 1), "members": members, "thermal_state": tstate, "free_gib": round(free/GIB, 2),
                                          "memory_pressure_level": pressure, "memory_free_pct": free_pct,
                                          "swap_mib": round(used_swap/MIB,1), "swap_baseline_mib": round(swap_baseline/MIB,1),
                                          "maintenance_pending": maintenance_reason or disk_stop_reason(free, a.min_free_gib*GIB),
                                          "diagnostics_capped": output_bytes > log.prefix_limit}), flush=True)
                        last_status = now
                    sample_at = now + 2
                if a.timeout and now - started > a.timeout:
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
                proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
            if not reported:
                print(json.dumps({"result": {"exit_code": proc.poll() if proc else None,
                    "reason": reason or "interrupted", "duration_s": round(time.monotonic()-started, 2),
                    "peak_sampled_rss_mib": round(max_rss/MIB, 1), "peak_sampled_cpu_pct": round(max_cpu, 1),
                    "output_bytes": output_bytes}}), flush=True)
    finally:
        log.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Guard failed closed: " + str(exc), file=sys.stderr)
        sys.exit(2)
