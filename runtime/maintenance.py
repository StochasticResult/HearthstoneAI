"""Local housekeeping at a game boundary; no game input, APIs or OS purging."""
import gc
import shutil
import time

from resource_guard import (GIB, MIB, battery_status, thermal_state, swap_used,
                            memory_free_percentage, memory_pressure_level, memory_stop_reason)


def cleanup(data, keep=5):
    """Only delete old app-owned run directories, never live game/system files."""
    root = data / 'runs'
    if root.is_symlink():
        raise RuntimeError('Run directory must not be a symlink')
    removed = 0
    runs = sorted(p for p in root.glob('run-*') if p.is_dir() and not p.is_symlink())
    for path in runs[:-keep]:
        shutil.rmtree(path)
        removed += 1
    return {'old_runs_removed': removed, 'collected_objects': gc.collect(), 'at': time.time()}


def readiness(baseline):
    """Stricter admission checks apply only when no game is in progress."""
    ac, battery = battery_status()
    current_swap = swap_used()
    pressure, available = memory_pressure_level(), memory_free_percentage()
    thermal = thermal_state()
    disk = shutil.disk_usage('/System/Volumes/Data').free
    reasons = []
    memory = memory_stop_reason(current_swap, baseline, pressure, available)
    if memory:
        reasons.append(memory)
    if thermal >= 1:
        reasons.append('waiting for nominal thermal state')
    if not ac:
        reasons.append('waiting for AC power')
    if disk < 10*GIB:
        reasons.append('disk reserve')
    return {'ready': not reasons, 'reasons': reasons, 'thermal_state': thermal,
            'ac': ac, 'battery_pct': battery, 'free_gib': round(disk/GIB, 2),
            'memory_pressure_level': pressure, 'memory_free_pct': available,
            'swap_mib': round(current_swap/MIB, 1),
            'swap_growth_mib': round((current_swap-baseline)/MIB, 1)}
