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


def consume_request(data):
    path = data / "strategy-eval-request.json"
    if not path.exists():
        return False
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError("棋力测试请求无效；未启动游戏")
    request = json.loads(path.read_text())
    path.unlink()
    if (set(request) != {"suite", "expires_at"} or request["suite"] not in ("strategy-v1", "model-comparison-v1", "hard-comparison-v1", "rule-transfer-v1", "tracking-v1")
            or type(request["expires_at"]) not in (int, float)
            or not 0 <= request["expires_at"] - time.time() <= 300):
        raise ValueError("棋力测试请求已过期或无效；未启动游戏")
    return request["suite"]


def run_evaluation(config, root, data, parent_pid, emit, stop_child, suite="strategy-v1"):
    from session import lock_session, drain_guards
    lock = lock_session()
    guard = caffeine = None
    def stop(_signum, _frame):
        raise RuntimeError("棋力测试已停止")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        comparison = suite == "model-comparison-v1"
        hard = suite == "hard-comparison-v1"
        transfer = suite == "rule-transfer-v1"
        tracking = suite == "tracking-v1"
        dest = data / ("tracking-evaluation" if tracking else "rule-transfer" if transfer else "hard-comparison" if hard else "model-comparison" if comparison else "evaluation")
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        emit("正在运行固定局面棋力评测：最多 %d 次 API 请求，不启动炉石、不发送游戏输入" % (4 if tracking else 48 if comparison or hard else 16))
        caffeine = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-t", "320", "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        guard = subprocess.Popen([sys.executable, str(root / "resource_guard.py"),
            "--log", str(dest / "evaluation.log"), "--timeout", "300", "--max-rss-mib", "200", "--max-cpu", "130",
            "--parent-pid", str(os.getpid()), "--", sys.executable,
            str(root / ("state-bridge/tracking_eval.py" if tracking else "state-bridge/rule_transfer.py" if transfer else "state-bridge/hard_comparison.py" if hard else "state-bridge/model_comparison.py" if comparison else "state-bridge/strategy_eval.py")), str(dest / "results.jsonl")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        guard.stdin.write((json.dumps(config) + "\n").encode()); guard.stdin.close()
        config["api_key"] = ""
        streams, sources = {guard.stdout: b""}, {guard.stdout: "棋力评测守护"}
        deadline = time.monotonic() + 310
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
            raise RuntimeError("棋力评测未完成；原因见 evaluation/evaluation.log")
        rows = (dest / "results.jsonl").read_text().splitlines()
        summary = json.loads(rows[-1]).get("summary") if rows else None
        if not summary:
            raise RuntimeError("棋力评测缺少完整结果")
        emit("棋力评测完成；固定局面成绩不是实战胜率", strategy_eval_summary=summary)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_child(guard)
        stop_child(caffeine)
        lock.close()
        emit("棋力评测进程已回收", stopped=True)
