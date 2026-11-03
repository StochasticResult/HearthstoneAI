"""Local housekeeping at a game boundary; no game input, APIs or OS purging."""
import gc
import re
import shutil
import subprocess
import time

from resource_guard import (GIB, MIB, battery_status, thermal_state, swap_used,
                            memory_free_percentage, memory_pressure_level, memory_stop_reason)

RESOURCE_CHECK_SECONDS = 15
MAX_WARNING_SWAPOUT_MIB_PER_MINUTE = 256
HEAVY_PAGING = 'active paging under memory pressure'


def swapout_bytes():
    report = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True,
                            timeout=3, check=True).stdout
    page_size = re.search(r'\(page size of (\d+) bytes\)', report)
    swapouts = re.search(r'^Swapouts:\s*(\d+)\.?\s*$', report, re.MULTILINE)
    if not page_size or not swapouts or int(page_size[1]) <= 0:
        raise ValueError('unreadable vm_stat swapout counters')
    return int(page_size[1]) * int(swapouts[1])


def warning_paging(previous):
    """Compare recent writes to swap while macOS is reporting memory pressure."""
    try:
        total = swapout_bytes()
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'swapout reading unavailable', {}
    now = time.monotonic()
    sample = {'swapout_bytes': total, 'paging_sampled_at': now}
    if previous is None or 'swapout_bytes' not in previous:
        return 'observing current paging', sample
    elapsed = now - previous['paging_sampled_at']
    delta = total - previous['swapout_bytes']
    if not RESOURCE_CHECK_SECONDS / 2 <= elapsed <= 3 * RESOURCE_CHECK_SECONDS or delta < 0:
        return 'observing current paging', sample
    rate = delta / MIB * 60 / elapsed
    sample['swapout_mib_per_minute'] = round(rate, 1)
    reason = HEAVY_PAGING if rate > MAX_WARNING_SWAPOUT_MIB_PER_MINUTE else None
    previous_rate = previous.get('swapout_mib_per_minute')
    if (reason and previous.get('memory_pressure_level') == 2 and HEAVY_PAGING in previous.get('reasons', ())
            and isinstance(previous_rate, (int, float))):
        sample['sustained_paging_mib_per_minute'] = [previous_rate, sample['swapout_mib_per_minute']]
    return reason, sample


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


def readiness(baseline, previous=None, *, ranked=False):
    """Stricter admission checks apply only when no game is in progress."""
    ac, battery = battery_status()
    current_swap = swap_used()
    pressure, available = memory_pressure_level(), memory_free_percentage()
    thermal = thermal_state()
    disk = shutil.disk_usage('/System/Volumes/Data').free
    reasons = []
    # Keep the original baseline for diagnostics. Allocated system swap can
    # stay high after pressure has cleared, even with no game process alive.
    memory = memory_stop_reason(current_swap, baseline, pressure, available, growth=None, defer_maintenance=ranked)
    if memory:
        reasons.append(memory)
    if thermal >= 1:
        reasons.append('waiting for nominal thermal state')
    if not ac:
        reasons.append('waiting for AC power')
    if disk < 10*GIB:
        reasons.append('disk reserve')
    paging = {}
    if pressure == 2 and (not reasons or (ranked and not memory)):
        wait_reason, paging = warning_paging(previous)
        if wait_reason:
            reasons.append(wait_reason)
    return {'ready': not reasons, 'reasons': reasons, 'thermal_state': thermal,
            'ac': ac, 'battery_pct': battery, 'free_gib': round(disk/GIB, 2),
            'memory_pressure_level': pressure, 'memory_free_pct': available,
            'swap_mib': round(current_swap/MIB, 1),
            'swap_growth_mib': round((current_swap-baseline)/MIB, 1), **paging}


WAIT_LABELS = {
    'critical system memory pressure': '系统内存压力严重',
    'system memory availability threshold': '系统可用内存指标过低',
    'unknown system memory status': '等待系统内存读数',
    'waiting for nominal thermal state': '等待电脑降温',
    'waiting for AC power': '等待接通电源',
    'disk reserve': '磁盘可用空间不足 10 GiB',
    'swapout reading unavailable': '等待系统换页读数',
    'observing current paging': '内存压力告警，观察 15 秒换页活动',
    HEAVY_PAGING: '内存压力告警，系统正在大量换页',
}
