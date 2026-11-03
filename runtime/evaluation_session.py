"""One-shot developer evaluation, through the app's existing Keychain identity.

The Practice button reports this queued mode explicitly. Offline self-check is
unchanged. A stale or invalid request stops, never launches an unintended game.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SUITES = {
    'strategy-v1': ('evaluation','strategy_eval.py',16,300),
    'model-comparison-v1': ('model-comparison','model_comparison.py',48,300),
    'hard-comparison-v1': ('hard-comparison','hard_comparison.py',48,300),
    'rule-transfer-v1': ('rule-transfer','rule_transfer.py',16,300),
    'tracking-v1': ('tracking-evaluation','tracking_eval.py',4,300),
    'sol-profiles-v1': ('sol-profile-evaluation','sol_profile_eval.py',24,480),
    'sol-fast-efforts-v1': ('sol-fast-efforts','sol_profile_eval.py',16,320),
    'luna-decisions-v1': ('decisions-sol-evaluation','luna_decisions_eval.py',32,620),
    'luna-decisions-holdout-v1': ('decisions-sol-holdout','luna_decisions_eval.py',36,690),
    'luna-decisions-recovery-v1': ('decisions-sol-recovery','luna_decisions_eval.py',25,480),
}


def consume_request(data):
    path = data / "strategy-eval-request.json"
    if not path.exists():
        return False
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError("棋力测试请求无效；未启动游戏")
    request = json.loads(path.read_text())
    path.unlink()
    if (set(request) != {"suite", "expires_at"} or request["suite"] not in SUITES
            or type(request["expires_at"]) not in (int, float)
            or not 0 <= request["expires_at"] - time.time() <= 300):
        raise ValueError("棋力测试请求已过期或无效；未启动游戏")
    return request["suite"]


def run_evaluation(config, root, data, parent_pid, emit, stop_child, suite="strategy-v1"):
    from session import lock_session, drain_guards
    lock = lock_session()
    guard = caffeine = app_wake = None
    cancelled = False
    def stop(_signum, _frame):
        nonlocal cancelled
        cancelled = True
        raise RuntimeError("棋力测试已停止")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        directory, script, requests, seconds = SUITES[suite]
        dest = data / directory
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        emit("固定局面评测 · 最多 %d 次 API 请求" % requests)
        if parent_pid and script == 'sol_profile_eval.py':
            from app_awake import AppWake
            if os.getppid() != parent_pid:
                raise RuntimeError('应用已退出，停止档位评测')
            app_wake = AppWake.acquire(data, parent_pid)
        else:
            caffeine = subprocess.Popen(["/usr/bin/caffeinate", "-di", "-t", str(seconds+20), "-w", str(os.getpid())],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        guard = subprocess.Popen([sys.executable, str(root / "resource_guard.py"),
            "--log", str(dest / "evaluation.log"), "--timeout", str(seconds), "--max-rss-mib", "200", "--max-cpu", "130",
            "--parent-pid", str(os.getpid()), "--", sys.executable,
            str(root / "state-bridge" / script), str(dest / "results.jsonl")]
            + {'sol-fast-efforts-v1':['--fast-efforts'], 'luna-decisions-holdout-v1':['--holdout'],
               'luna-decisions-recovery-v1':['--recovery']}.get(suite, []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        guard.stdin.write((json.dumps(config) + "\n").encode()); guard.stdin.close()
        config["api_key"] = ""
        streams, sources = {guard.stdout: b""}, {guard.stdout: "棋力评测守护"}
        deadline = time.monotonic() + seconds + 10
        while guard.poll() is None and time.monotonic() < deadline:
            if parent_pid and os.getppid() != parent_pid:
                raise RuntimeError("应用已退出，停止棋力测试")
            drain_guards(streams, sources)
            time.sleep(.5)
        if guard.poll() is None:
            raise RuntimeError("棋力测试达到时间上限")
        for _ in range(3):
            drain_guards(streams, sources)
        if guard.returncode:
            raise RuntimeError("棋力评测未完成，运行记录见 %s/evaluation.log" % directory)
        rows = (dest / "results.jsonl").read_text().splitlines()
        summary = json.loads(rows[-1]).get("summary") if rows else None
        if not summary:
            raise RuntimeError("棋力评测缺少完整结果")
        emit("棋力评测完成；固定局面成绩不是实战胜率", strategy_eval_summary=summary)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_child(guard)
        if guard:
            for handle in (guard.stdin, guard.stdout):
                if handle and not handle.closed:
                    handle.close()
        stop_child(caffeine)
        if app_wake and cancelled:
            app_wake.release()
        lock.close()
        emit("棋力评测进程已回收", stopped=True)
