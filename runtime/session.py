#!/usr/bin/env python3
"""Independent, single-instance application session; no Codex tools required."""
import argparse
import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from input_command import input_command
from game_watchdog import GAME_EXEC, processes
from navigation import FRAMEWORK, Stop as SupervisorError, next_navigation, read_json_line, stop_child

ROOT = Path(__file__).resolve().parent
DATA = Path.home() / "Library/Application Support/HearthstoneAI"
BATTLENET_EXEC = "/Applications/Battle.net.app/Contents/MacOS/Battle.net"
_event_log = None


class SessionError(Exception):
    pass


class EventLog:
    """Bounded local diagnostics; callers must never pass configuration/secrets."""
    def __init__(self, path, limit=1024 * 1024):
        self.handle = path.open("wb")
        self.limit, self.written = limit, 0

    def write(self, event):
        line = (json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event},
                           ensure_ascii=False) + "\n").encode()
        if self.written + len(line) <= self.limit:
            self.handle.write(line)
            self.handle.flush()
            self.written += len(line)

    def close(self):
        self.handle.close()


def emit(message, **fields):
    event = {"message": message, **fields}
    if _event_log:
        _event_log.write(event)
    try:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        pass  # Parent exit must not prevent resource cleanup.


def guard_event(line, source):
    try:
        event = json.loads(line)
    except ValueError:
        if line.strip():
            emit("%s：%s" % (source, line.decode("utf-8", "replace")[:1024]))
        return
    if not isinstance(event, dict):
        raise SessionError("资源监控输出异常")
    if "watchdog_result" in event:
        message = "游戏守护结束：%s" % event["watchdog_result"]
    elif isinstance(event.get("result"), dict):
        result = event["result"]
        message = "AI 守护结束：%s（退出码 %s）" % (result.get("reason") or "子进程退出", result.get("exit_code"))
    elif "restored_target_fps" in event:
        message = "已恢复原帧率：%s FPS" % event["restored_target_fps"]
    elif "claimed_pid" in event:
        message = "已确认炉石进程启动"
    elif "rss_mib" in event:
        message = "资源采样"
    else:
        message = "%s状态" % source
    emit(message, source=source, metrics=event)


def drain_guards(streams, sources):
    for stream in select.select(list(streams), [], [], 0)[0]:
        chunk = os.read(stream.fileno(), 8192)
        data = streams[stream] + chunk
        lines = data.split(b"\n")
        streams[stream] = lines.pop()
        if not chunk:
            if streams[stream]:
                lines.append(streams[stream])
            streams.pop(stream)
        elif len(streams[stream]) > 16384:
            raise SessionError("资源监控输出异常")
        for line in lines:
            guard_event(line, sources[stream])


def launch_hearthstone():
    # Battle.net supplies the authenticated game launch. Opening the game
    # bundle directly did not start a game on the tested Mac. This fixed
    # product command is also used by HearthSim's desktop tracker.
    subprocess.run([BATTLENET_EXEC, "--exec=launch WTCG"], timeout=8, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def validate_config(config):
    if not isinstance(config, dict) or not set(config) <= {"api_key", "model"}:
        raise SessionError("Invalid application settings")
    if not isinstance(config.get("api_key"), str) or not 20 <= len(config["api_key"]) <= 512:
        raise SessionError("请先在应用中填写 OpenAI API Key")
    if any(c.isspace() for c in config["api_key"]):
        raise SessionError("API Key 含有空白字符")
    model = config.get("model", "gpt-6.1-terra")
    import re
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", model):
        raise SessionError("模型名称无效")
    return {"api_key": config["api_key"], "model": model}


def preflight():
    if sys.version_info < (3, 12):
        raise SessionError("本版日志解析器需要 Python 3.12 或更高版本")
    if processes(GAME_EXEC):
        raise SessionError("请先退出炉石；软件需要从启动前建立资源保护")
    required = [Path(FRAMEWORK), Path(GAME_EXEC), Path(BATTLENET_EXEC), Path(input_command()[0]), ROOT / "mirror-status"]
    if any(not p.is_file() for p in required):
        raise SessionError("缺少炉石、HSTracker 或程序组件")
    result = subprocess.run(input_command("access"), capture_output=True, text=True, timeout=3)
    if result.returncode or result.stdout.strip() != "true":
        raise SessionError("请在系统设置中为 Hearthstone AI 开启辅助功能权限")


def lock_session():
    DATA.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    lock = (DATA / "session.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SessionError("已有一个测试正在运行") from None
    return lock


def session_directory():
    runs = DATA / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    previous = sorted((p for p in runs.glob("run-*") if p.is_dir() and not p.is_symlink()), key=lambda p: p.name)
    # This directory belongs only to this app; retain five bounded captures.
    for old in previous[:-4]:
        shutil.rmtree(old)
    dest = runs / time.strftime("run-%Y%m%d-%H%M%S")
    dest.mkdir(mode=0o700)
    return dest


def read_status():
    try:
        result = subprocess.run([str(ROOT / "mirror-status"), FRAMEWORK],
                                capture_output=True, text=True, timeout=4)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (subprocess.SubprocessError, ValueError):
        return None


def scene_ready(status):
    scene = status.get("scene") or {}
    return (scene.get("mode") in (3, 4, 13, 16) and scene.get("sceneLoaded") is True
            and not scene.get("transitioning"))


def navigation_click(point):
    geometry = subprocess.run(input_command("geometry"),
                              capture_output=True, text=True, timeout=3, check=True)
    frame = json.loads(geometry.stdout)
    if any(abs(float(frame[k]) - v) > 2 for k, v in {"x": 0, "y": 33, "width": 1470, "height": 846}.items()):
        raise SessionError("游戏窗口与已校准的 1470×846 布局不同，已停止操作")
    subprocess.run(input_command("click", *point),
                   check=True, timeout=4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(config, parent_pid):
    global _event_log
    lock = lock_session()
    watchdog = driver = caffeine = None
    tail = None
    streams, sources = {}, {}
    def interrupted(_signal, _frame):
        raise SessionError("已收到停止指令")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        preflight()
        dest = session_directory()
        _event_log = EventLog(dest / "session-events.jsonl")
        emit("正在建立资源保护", log_directory=str(dest))
        watchdog = subprocess.Popen([sys.executable, str(ROOT / "game_watchdog.py"),
            "--startup-seconds", "120", "--run-seconds", "300", "--temporary-fps", "30",
            "--parent-pid", str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        armed = read_json_line(watchdog, 10)
        guard_event(json.dumps(armed).encode(), "游戏守护")
        if armed.get("watchdog") != "armed":
            raise SessionError("游戏资源保护启动失败")
        driver = subprocess.Popen([sys.executable, str(ROOT / "resource_guard.py"),
            "--log", str(dest / "actions.log"), "--timeout", "430", "--max-rss-mib", "200", "--max-cpu", "130",
            "--parent-pid", str(os.getpid()), "--", "/usr/bin/nice", "-n", "10", sys.executable,
            str(ROOT / "state-bridge/practice_driver.py"), "--new-session", "--evidence", str(dest / "Power.log"),
            "--watchdog-pid", str(watchdog.pid), "--seconds", "300", "--max-actions", "80", "--openai-config-stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        driver.stdin.write((json.dumps(config) + "\n").encode())
        driver.stdin.close()
        config["api_key"] = ""
        checked, started = read_json_line(driver, 10), read_json_line(driver, 10)
        guard_event(json.dumps(checked).encode(), "AI 守护")
        guard_event(json.dumps(started).encode(), "AI 守护")
        if "preflight" not in checked or "started_pid" not in started:
            raise SessionError("AI 进程资源保护启动失败")
        streams = {p.stdout: getattr(p, "_pending_line_bytes", b"") for p in (watchdog, driver)}
        sources = {watchdog.stdout: "游戏守护", driver.stdout: "AI 守护"}
        caffeine = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-t", "450", "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Establish the driver's log baseline before launching. This is the
        # only application launched; no global app-switch shortcut is used.
        time.sleep(.8)
        if watchdog.poll() is not None or driver.poll() is not None:
            raise SessionError("启动前资源保护已经停止")
        launch_hearthstone()
        emit("已向战网请求启动炉石，正在等待实际游戏进程和主界面")
        stage, last_click, focused, in_game = 0, 0, False, False
        previous_scene = None
        deadline = time.monotonic() + 430
        tail = (dest / "actions.log").open("rb")
        log_buffer = b""
        while time.monotonic() < deadline:
            if parent_pid and os.getppid() != parent_pid:
                raise SessionError("应用已退出，停止本次运行")
            # Drain child pipes so resource telemetry can never block a guard.
            drain_guards(streams, sources)
            log_buffer += tail.read(8192)
            lines = log_buffer.split(b"\n")
            log_buffer = lines.pop()
            if len(log_buffer) > 16384:
                raise SessionError("动作日志输出异常")
            for line in lines:
                if line:
                    emit(line.decode("utf-8", "replace")[:2048])
            if driver.poll() is not None:
                if driver.returncode != 0:
                    raise SessionError("AI 已停止，原因见上方运行记录")
                emit("本次人机测试结束")
                return
            if watchdog.poll() is not None:
                raise SessionError("游戏资源保护已结束运行，原因见上方守护记录")
            if not in_game:
                status = read_status()
                if status:
                    scene = status.get("scene") or {}
                    summary = {k: scene.get(k) for k in ("mode", "sceneLoaded", "transitioning")}
                    if summary != previous_scene:
                        emit("游戏场景更新", scene=summary)
                        previous_scene = summary
                    # Mirror may attach before Unity has created a window.
                    # Do not attempt foreground/input until a known scene loads.
                    if not scene_ready(status):
                        time.sleep(.5)
                        continue
                    if not focused:
                        subprocess.run(input_command("focus"), check=True, timeout=3,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        focused = True
                    if scene.get("mode") == 4:
                        if status.get("game_type") != 1:
                            raise SessionError("当前对局不是练习模式，停止")
                        in_game = True
                        emit("已进入人机，AI 正在自主运行")
                    else:
                        point = next_navigation(stage, status)
                        if point and time.monotonic() - last_click > 6:
                            navigation_click(point)
                            stage += 1
                            last_click = time.monotonic()
                            emit("正在进入练习模式（%d/6）" % stage)
            time.sleep(.5)
        raise SessionError("测试达到时间上限")
    finally:
        # Block repeated stop signals while finally restores settings.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for child in (driver, watchdog, caffeine):
            stop_child(child)
        for child, label in ((watchdog, "游戏守护"), (driver, "AI 守护")):
            if child and child.stdout and child.stdout not in sources:
                streams[child.stdout] = getattr(child, "_pending_line_bytes", b"")
                sources[child.stdout] = label
        # Collect the final reason and FPS restoration after guards exit.
        for _ in range(8):
            if not streams:
                break
            drain_guards(streams, sources)
        if tail:
            tail.close()
        lock.close()
        emit("已停止：子进程已回收，设置恢复结果见上方记录", stopped=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    config = validate_config(json.loads(sys.stdin.readline(4097)))
    from evaluation_session import consume_request, run_evaluation
    if consume_request(DATA):
        run_evaluation(config, ROOT, DATA, args.parent_pid, emit, stop_child)
    else:
        run(config, args.parent_pid)


if __name__ == "__main__":
    try:
        main()
    except (SessionError, SupervisorError, OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        emit(str(error), error=True)
        sys.exit(2)
    finally:
        if _event_log:
            _event_log.close()
