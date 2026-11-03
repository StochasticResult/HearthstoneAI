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
from deck_observation import save_selected_deck
from game_watchdog import GAME_EXEC, processes
from navigation import FRAMEWORK, Stop as SupervisorError, login_intro_page, next_navigation, next_ranked_navigation, read_json_line, stop_child
from game_mode import expected_type
from game_boundary import FaultedMatchEnd, LogBoundary, result_dismissal
from ranked_focus import FocusLost, PointCovered, check_point, rejected_before_input

ROOT = Path(__file__).resolve().parent
DATA = Path.home() / "Library/Application Support/HearthstoneAI"
BATTLENET_EXEC = "/Applications/Battle.net.app/Contents/MacOS/Battle.net"
_event_log = None


class SessionError(Exception):
    pass


class SessionCancelled(SessionError):
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
    return event


def drain_guards(streams, sources, observer=None):
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
            event = guard_event(line, sources[stream])
            if observer and isinstance(event,dict):
                observer(event, sources[stream])


def launch_hearthstone():
    # Battle.net supplies the authenticated game launch. Opening the game
    # bundle directly did not start a game on the tested Mac. This fixed
    # product command is also used by HearthSim's desktop tracker.
    subprocess.run([BATTLENET_EXEC, "--exec=launch WTCG"], timeout=8, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def validate_config(config):
    if not isinstance(config, dict) or not set(config) <= {"api_key", "model", "mode"}:
        raise SessionError("Invalid application settings")
    if not isinstance(config.get("api_key"), str) or not 20 <= len(config["api_key"]) <= 512:
        raise SessionError("请先在应用中填写 OpenAI API Key")
    if any(c.isspace() for c in config["api_key"]):
        raise SessionError("API Key 含有空白字符")
    model = config.get("model", "gpt-6.1-sol")
    import re
    profile = {}
    if isinstance(model, str) and ' ' in model.strip():
        parts = model.split()
        if len(parts) not in (2,3) or parts[0] != 'gpt-6.1-sol':
            raise SessionError("模型配置无效，可用示例：gpt-6.1-sol fast low")
        for setting in parts[1:]:
            if setting in ('standard','fast') and 'service_tier' not in profile:
                profile['service_tier'] = 'priority' if setting == 'fast' else 'default'
            elif setting in ('low','medium','high','xhigh','max') and 'reasoning_effort' not in profile:
                profile['reasoning_effort'] = setting
            else:
                raise SessionError("模型的速度或推理档位无效")
        model = parts[0]
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", model):
        raise SessionError("模型名称无效")
    result = {"api_key": config["api_key"], "model": model, **profile}
    if 'mode' in config:
        if config['mode'] not in ('practice', 'ranked', 'ranked_auto'):
            raise SessionError("游戏模式无效")
        result['mode'] = config['mode']
    return result


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
    return (scene.get("mode") in (3, 4, 7, 13, 16) and scene.get("sceneLoaded") is True
            and not scene.get("transitioning"))


def focus_hearthstone():
    # LaunchServices brings the already-observed game app forward. A short-
    # lived input helper's NSRunningApplication.activate request can succeed
    # without activation on current macOS; it is not a foreground guarantee.
    # This is called once, only after the watchdog and a known loaded scene.
    subprocess.run(["/usr/bin/open", "-a", str(Path(GAME_EXEC).parents[2])],
                   check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def navigation_click(point, *, managed=False, game_pid=None):
    geometry = subprocess.run(input_command("geometry"),
                              capture_output=True, text=True, timeout=3, check=not managed)
    if managed and geometry.returncode:
        if geometry.stderr.strip() == 'Could not uniquely locate a visible Hearthstone window':
            raise FocusLost('炉石窗口暂不可见')
        geometry.check_returncode()
    frame = json.loads(geometry.stdout)
    if any(abs(float(frame[k]) - v) > 2 for k, v in {"x": 0, "y": 33, "width": 1470, "height": 846}.items()):
        raise SessionError("游戏窗口与已校准的 1470×846 布局不同，已停止操作")
    # Activating an app is asynchronous. A native foreground rejection occurs
    # before any mouse event; only that exact rejection may be retried briefly.
    # Never retry a successful click or an uncertain input failure.
    deadline = time.monotonic() + .8
    while True:
        if game_pid is not None:
            check_point(game_pid, *point)
        result = subprocess.run(input_command("click", *point),
                                capture_output=True, text=True, timeout=4)
        if result.returncode == 0:
            return
        detail = result.stderr.strip()
        if managed and rejected_before_input(detail):
            raise FocusLost('炉石失去前台，点击未发送')
        if detail != "Hearthstone is not the frontmost app" or time.monotonic() >= deadline:
            raise SessionError("导航输入失败：" + detail.replace("\n", " ")[:180])
        time.sleep(.15)


class PersistentClient:
    """Own the client/watchdog once; per-game workers never close them."""
    def __init__(self, parent_pid, swap_baseline):
        self.parent_pid, self.swap_baseline = parent_pid, swap_baseline
        self.watchdog = self.lock = None
        self.streams, self.sources = {}, {}
        self.pid = None
        self.launched = False
        self.previous = None
        self.boundary = None
        self.on_match_hold = None
        self.match_at_risk = False
        self.focus = self.on_foreground_change = None
        self.last_focus_state = None
        self.covered_point = None

    def foreground_ready(self):
        if self.focus is None:
            return True
        if self.pid is None:
            return False
        self.focus.bind(self.pid)
        code = self.focus.state()
        if code == b'S':
            raise SessionCancelled('已收到停止指令')
        if code == b'X':
            raise SessionError('原炉石进程身份改变')
        if code != self.last_focus_state:
            if code in (b'W', b'L', b'F'):
                attempts = int.from_bytes(os.pread(self.focus.file.fileno(), 4, 5), sys.byteorder)
                if self.on_foreground_change:
                    self.on_foreground_change(code, attempts)
                message = {b'W':'正在自动取回炉石前台', b'L':'等待 Mac 解锁 · 炉石保持运行',
                           b'F':'炉石已在前台 · 继续自动天梯'}[code]
                emit(message, play_status=message, game_pid=self.pid, foreground_requests=attempts)
            self.last_focus_state = code
        return code == b'F'

    def click_if_ready(self, point, alternatives=()):
        if self.focus is None:
            navigation_click(point)
            return True
        if not self.foreground_ready():
            return False
        for candidate in (point, *alternatives):
            try:
                navigation_click(candidate, managed=True, game_pid=self.pid)
                if self.covered_point is not None or candidate != point:
                    emit('已在炉石自己的窗口中恢复点击', game_pid=self.pid, navigation_point=candidate)
                self.covered_point = None
                return True
            except PointCovered:
                continue  # Only another point on the same verified control.
            except FocusLost:
                return False
        if self.covered_point != point:
            self.covered_point = point
            emit('按钮位置受到其他窗口遮挡，等待炉石可点击位置', play_status='等待按钮露出 · 炉石保持运行')
        return False

    def observe(self, event, source):
        if source == '游戏守护' and type(event.get('claimed_pid')) is int:
            if self.pid is not None and self.pid != event['claimed_pid']:
                raise SessionError('游戏进程身份改变')
            self.pid = event['claimed_pid']

    def prepare(self):
        if self.watchdog is None:
            self.lock = lock_session()
            preflight()
            owner_args = ['--native-owner-pid',str(self.parent_pid)] if self.parent_pid else []
            self.watchdog = subprocess.Popen([sys.executable, str(ROOT/'game_watchdog.py'),
                '--startup-seconds','120','--run-seconds','0','--temporary-fps','30',
                '--parent-pid',str(os.getpid()),'--swap-baseline-mib',str(self.swap_baseline/(1024**2))] + owner_args,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            armed=read_json_line(self.watchdog,10)
            guard_event(json.dumps(armed).encode(),'游戏守护')
            if armed.get('watchdog') != 'armed':
                raise SessionError('游戏资源保护启动失败')
            self.streams={self.watchdog.stdout:getattr(self.watchdog,'_pending_line_bytes',b'')}
            self.sources={self.watchdog.stdout:'游戏守护'}
        else:
            self.return_to_menu()

    def tick(self):
        if self.parent_pid and os.getppid() != self.parent_pid:
            raise SessionError('应用已退出，停止本次运行')
        if self.watchdog:
            drain_guards(self.streams,self.sources,self.observe)
            if self.watchdog.poll() is not None:
                raise SessionError('常驻游戏资源保护已停止')
        if self.pid is not None and not self.alive():
            raise SessionError('原有炉石进程已退出；不自动启动另一份客户端')

    def alive(self):
        return (self.watchdog is not None and self.watchdog.poll() is None and self.pid is not None
                and [p[0] for p in processes(GAME_EXEC)] == [self.pid])

    def return_to_menu(self):
        if not self.previous or self.previous.get('outcome') not in ('WON','LOST','TIED') or self.boundary is None:
            self.match_at_risk = True
            raise SessionError('上一局未核实结束，不执行结算或排队操作')
        from ranked_autoplay import AfterGameStop, read_small
        last_click=None
        emit('等待已结束对局的结算；自动点击继续，炉石保持运行',play_status='等待结算 · 自动继续')
        while True:
            self.tick()
            try:
                self.boundary.check()
            except (OSError, SupervisorError):
                # The old result no longer proves it's safe to close this PID.
                self.match_at_risk = True
                raise
            if read_small(DATA/'learning/pause-after-game.json'):
                raise AfterGameStop()
            if not self.foreground_ready():
                time.sleep(.4)
                continue
            status=read_status()
            if status and (status.get('scene') or {}).get('mode')==9:
                raise SessionError('炉石客户端在结算时进入错误场景')
            if status and scene_ready(status):
                mode=status['scene']['mode']
                if mode==7:
                    # Validate exactly the same Standard/deck gate as first launch.
                    if next_ranked_navigation(1,status):
                        emit('已返回标准选牌；保留原炉石进程', game_pid=self.pid)
                        return
                point = next_ranked_navigation(0,status) if mode==3 else result_dismissal(status,self.previous['outcome'])
                if point and (last_click is None or time.monotonic()-last_click>=5):
                    # Confirmed result label in the 1470×846 window. Its lower
                    # edge can be covered by a floating remote-viewing control.
                    alternatives = ()
                    if mode == 4 and point == (735,845) and self.focus is not None:
                        point, alternatives = (735,833), (point,)
                    if self.click_if_ready(point, alternatives):
                        last_click=time.monotonic()
                        emit('正在退出已结束的对局结算',game_pid=self.pid)
            time.sleep(1)

    def driver_log_args(self):
        if not self.launched:
            return ['--new-session']
        if self.boundary is None:
            self.match_at_risk = True
            raise SessionError('缺少下一局日志边界')
        try:
            offset = self.boundary.queue_offset()
        except (OSError, SupervisorError):
            self.match_at_risk = True
            raise
        return ['--log',str(self.boundary.path),'--start-offset',str(offset)]

    def remember(self, dest, review):
        self.match_at_risk = True
        path=dest/'log-cursor.json'
        if path.is_symlink() or path.stat().st_size>2048:
            raise SessionError('单局日志边界无效')
        self.boundary=LogBoundary(json.loads(path.read_text()))
        self.previous=review
        self.match_at_risk = review.get('outcome') not in ('WON','LOST','TIED')

    def hold_after_ai_fault(self, dest, streams, sources, entered_game):
        """Keep the game and its resource guard alive; the failed AI sends no input."""
        if self.focus is not None:
            self.focus.bind(0)
        emit('AI 后台暂停，保留当前对局和资源保护，可手动接手',
             play_status='AI 暂停 · 炉石保持运行', game_pid=self.pid, holding_match=True)
        if self.on_match_hold:
            try:
                self.on_match_hold()
            except OSError:
                pass
        observer = None
        receipt = dest / 'log-cursor.json'
        try:
            if receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size <= 2048:
                observer = FaultedMatchEnd(json.loads(receipt.read_bytes()))
        except (OSError, ValueError, KeyError, TypeError, SupervisorError):
            pass  # Without a verified log, only a stable return from the game to the lobby suffices.
        lobby_samples = 0
        next_scene_read = 0
        while self.watchdog.poll() is None:
            if self.parent_pid and os.getppid() != self.parent_pid:
                self.match_at_risk = False
                raise SessionCancelled('应用已退出，停止本次运行')
            drain_guards(streams, sources, self.observe)
            if observer is not None:
                try:
                    if observer.poll():
                        self.match_at_risk = False
                        emit('已核实本局自然结算，结束故障观察', match_ended_after_fault=True)
                        try:
                            replay = subprocess.run([sys.executable,str(ROOT/'state-bridge/held_result.py'),
                                                     str(dest),str(observer.offset)], timeout=8,
                                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                            return replay.returncode == 0
                        except (OSError,subprocess.SubprocessError):
                            return False
                except (OSError, ValueError, SupervisorError):
                    observer = None
            if time.monotonic() >= next_scene_read:
                next_scene_read = time.monotonic() + 2
                status = read_status() or {}
                mode = (status.get('scene') or {}).get('mode')
                entered_game = entered_game or mode == 4
                in_lobby = entered_game and mode in (3,7) and scene_ready(status)
                lobby_samples = lobby_samples + 1 if in_lobby else 0
                if lobby_samples >= 2:
                    self.match_at_risk = False
                    emit('已核实对局返回大厅，结束故障观察', match_left_after_fault=True)
                    return
            time.sleep(.5)

    def close(self):
        try:
            if self.boundary and self.parent_pid and not self.match_at_risk and self.watchdog and self.watchdog.poll() is None:
                try:
                    self.boundary.check()
                except (OSError, SupervisorError):
                    self.match_at_risk = True
            if self.match_at_risk and self.parent_pid and self.watchdog and self.watchdog.poll() is None:
                # Even a second exception during fault observation must not
                # turn AI teardown into SIGTERM for a live ranked game.
                try:
                    self.watchdog.send_signal(signal.SIGUSR1)
                except ProcessLookupError:
                    pass
            else:
                stop_child(self.watchdog)
            if self.watchdog and self.watchdog.stdout and not self.watchdog.stdout.closed:
                for _ in range(8):
                    if not self.streams:break
                    drain_guards(self.streams,self.sources)
        finally:
            if self.watchdog and self.watchdog.stdout:
                self.watchdog.stdout.close()
            self.streams.clear()
            if self.lock:self.lock.close();self.lock=None


def run(config, parent_pid, mode="practice", swap_baseline=None, client=None):
    global _event_log
    expected = expected_type(mode)[1]
    label = "标准天梯" if mode == "ranked" else "人机练习"
    lock = lock_session() if client is None else None
    watchdog = driver = caffeine = None
    tail = None
    dest = None
    review_summary = None
    streams, sources = {}, {}
    finished = False
    stopped_before_queue = False
    from ranked_autoplay import AfterGameStop, read_small
    in_game = queue_attempted = False
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def interrupted(_signal, _frame):
        if client:
            client.match_at_risk = False
        raise SessionCancelled("已收到停止指令")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if client is None:
            preflight()
        else:
            # Returning from a completed game is not a new match. In
            # particular, a user's stop during a long result animation must
            # not create a spurious INCOMPLETE game in the match history.
            client.prepare()
            watchdog=client.watchdog
        dest = session_directory()
        _event_log = EventLog(dest / "session-events.jsonl")
        from build_identity import current_build
        from match_review import save
        from coach_policy import snapshot
        build=current_build(ROOT)
        config['compact_output'] = True
        policy = snapshot(DATA/'learning')
        save(dest/'run-policy.json',policy)
        build['coach_revision'] = policy['revision']
        build['play_settings'] = {k:policy[k] for k in ('multi_step','standby','standby_delay')}
        build['inference'] = {'model':config.get('model','gpt-6.1-sol'),
                              'tier':config.get('service_tier','auto'),
                              'effort':config.get('reasoning_effort','low'), 'output_style':'tag'}
        save(dest/'run-version.json',build)
        emit('本局程序版本：'+build['version_id'],runtime_version=build['version_id'])
        emit('推理设置 · %s · %s · %s' % (build['inference']['model'],build['inference']['tier'],build['inference']['effort']))
        emit("本局模式：%s" % label, game_mode=mode)
        emit("正在建立资源保护：不设对局时长、动作次数或累计 API 配额；保留内存、温度与电源检查",
             log_directory=str(dest), limits={"game_seconds": None, "api_requests": None, "api_tokens": None})
        baseline_args = [] if swap_baseline is None else ['--swap-baseline-mib', str(swap_baseline / (1024**2))]
        if client is None:
            watchdog = subprocess.Popen([sys.executable, str(ROOT / "game_watchdog.py"),
            "--startup-seconds", "120", "--run-seconds", "0", "--temporary-fps", "30",
            "--parent-pid", str(os.getpid())] + baseline_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            armed = read_json_line(watchdog, 10)
            guard_event(json.dumps(armed).encode(), "游戏守护")
            if armed.get("watchdog") != "armed":
                raise SessionError("游戏资源保护启动失败")
        reconnect_args = []
        for previous in sorted(dest.parent.glob("run-*"), reverse=True):
            if previous != dest and previous.is_dir() and not previous.is_symlink() and (previous / "Power.log").is_file():
                reconnect_args.extend(["--reconnect-evidence", str(previous / "Power.log")])
            if len(reconnect_args) == 8:
                break
        log_args = client.driver_log_args() if client else ['--new-session']
        managed_focus = client.focus if client is not None else None
        if managed_focus is not None:
            log_args += ['--foreground-lease', managed_focus.file.name]
        driver = subprocess.Popen([sys.executable, str(ROOT / "resource_guard.py"),
            "--log", str(dest / "actions.log"), "--timeout", "0", "--max-rss-mib", "200", "--max-cpu", "130",
            "--defer-maintenance", "--parent-pid", str(os.getpid())] + baseline_args + ["--", "/usr/bin/nice", "-n", "10", sys.executable,
            str(ROOT / "state-bridge/practice_driver.py")] + log_args + ["--evidence", str(dest / "Power.log"),
            "--deck-snapshot", str(dest / "selected-deck.json"),
            "--watchdog-pid", str(watchdog.pid), "--seconds", "0", "--max-actions", "0", "--mode", mode, "--openai-config-stdin"] + reconnect_args,
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
        if client:
            streams[watchdog.stdout]=client.streams.get(watchdog.stdout,b'')
        sources = {watchdog.stdout: "游戏守护", driver.stdout: "AI 守护"}
        if swap_baseline is None:  # Autoplay owns one assertion across all games and resource waits.
            caffeine = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-w", str(os.getpid())],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Establish the driver's log baseline before launching. This is the
        # only application launched; no global app-switch shortcut is used.
        time.sleep(.8)
        if watchdog.poll() is not None or driver.poll() is not None:
            raise SessionError("启动前资源保护已经停止")
        reuse=bool(client and client.launched)
        launch_time = None
        last_intro_page = None
        next_intro_check = 0
        if not reuse:
            launch_time = time.time()
            launch_hearthstone()
            if client:client.launched=True
            emit("已向战网请求启动炉石，正在等待实际游戏进程和主界面")
        else:
            emit('本局复用炉石客户端，仅新建 AI 与解析后台',game_pid=client.pid)
        stage, last_click, focused = (1 if reuse else 0), 0, reuse
        previous_scene = None
        tail = (dest / "actions.log").open("rb")
        log_buffer = b""
        while True:
            if parent_pid and os.getppid() != parent_pid:
                raise SessionError("应用已退出，停止本次运行")
            # Drain child pipes so resource telemetry can never block a guard.
            drain_guards(streams, sources, client.observe if client else None)
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
                emit("本次%s结束" % label)
                finished=True
                break
            if watchdog.poll() is not None:
                raise SessionError("游戏资源保护已结束运行，原因见上方守护记录")
            if (client and client.previous and not in_game and not queue_attempted
                    and read_small(DATA/'learning/pause-after-game.json')):
                raise AfterGameStop()
            if managed_focus is not None and managed_focus.target and not client.foreground_ready():
                time.sleep(.4)
                continue
            if not in_game:
                status = read_status()
                if status:
                    if save_selected_deck(status, dest / "selected-deck.json"):
                        emit("已从选牌菜单读取完整 30 张牌表")
                    scene = status.get("scene") or {}
                    summary = {k: scene.get(k) for k in ("mode", "sceneLoaded", "transitioning")}
                    if summary != previous_scene:
                        emit("游戏场景更新", scene=summary)
                        previous_scene = summary
                    # Mirror may attach before Unity has created a window.
                    # Do not attempt foreground/input until a known scene loads.
                    if not scene_ready(status):
                        if launch_time is not None and time.monotonic() >= next_intro_check:
                            next_intro_check = time.monotonic() + 2
                            page = login_intro_page(status, launch_time)
                            if page is not None and page != last_intro_page:
                                if managed_focus is None:
                                    focus_hearthstone()
                                elif not client.foreground_ready():
                                    time.sleep(.4)
                                    continue
                                if login_intro_page(read_status() or {}, launch_time) == page:
                                    # Empty backdrop, outside the cards and central controls.
                                    if managed_focus is None:
                                        navigation_click((180,155))
                                    elif not client.click_if_ready((180,155)):
                                        continue
                                    last_intro_page = page
                                    emit('已点按登录展示的空白背景，等待新页面或主菜单')
                        time.sleep(.5)
                        continue
                    launch_time = None
                    if managed_focus is not None:
                        if not client.foreground_ready():
                            time.sleep(.4)
                            continue
                    elif not focused:
                        focus_hearthstone()
                        focused = True
                    if scene.get("mode") == 4:
                        if client:
                            client.match_at_risk = True
                        if status.get("game_type") != expected or (mode == "ranked" and status.get("format") != 2):
                            raise SessionError("当前对局与请求的模式不符，停止")
                        in_game = True
                        emit("已进入%s，AI 正在自主运行" % label)
                    else:
                        point = (next_ranked_navigation if mode == "ranked" else next_navigation)(stage, status)
                        if point and time.monotonic() - last_click > 6:
                            if mode == 'ranked' and stage == 1:
                                queue_attempted = True
                                if client:
                                    client.match_at_risk = True
                            if managed_focus is None:
                                navigation_click(point)
                            elif not client.click_if_ready(point):
                                # This particular click definitely never started. Do not
                                # advance the navigation stage or claim an online match.
                                if stage == 1:
                                    queue_attempted = client.match_at_risk = False
                                time.sleep(.4)
                                continue
                            stage += 1
                            last_click = time.monotonic()
                            emit("正在进入%s（%d/%d）" % (label, stage, 2 if mode == "ranked" else 6))
            time.sleep(.5)
    except AfterGameStop:
        stopped_before_queue = not (in_game or queue_attempted)
        raise
    except SessionCancelled:
        raise
    except Exception:
        verified_end = False
        if (client and dest and watchdog and watchdog.poll() is None
                and (not parent_pid or os.getppid() == parent_pid)):
            if not (in_game or queue_attempted):
                status = read_status() or {}
                in_game = (status.get('scene') or {}).get('mode') == 4
            if in_game or queue_attempted:
                client.match_at_risk = True
                stop_child(driver)
                verified_end = client.hold_after_ai_fault(dest, streams, sources, in_game)
        if not verified_end:
            raise
    finally:
        # Block repeated stop signals while finally restores settings.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        retained=bool(client and finished)
        keep_guard = retained or bool(client and client.parent_pid and client.match_at_risk)
        for child in (driver, None if keep_guard else watchdog, caffeine):
            stop_child(child)
        for child, label in ((watchdog, "游戏守护"), (driver, "AI 守护")):
            if child and child.stdout and child.stdout not in sources:
                streams[child.stdout] = getattr(child, "_pending_line_bytes", b"")
                sources[child.stdout] = label
        # Collect the final reason and FPS restoration after guards exit.
        for _ in range(8):
            if not streams:
                break
            drain_guards(streams, sources, client.observe if client else None)
        if client and watchdog:
            client.streams={s:b for s,b in streams.items() if s is watchdog.stdout}
        if tail:
            tail.close()
        for child in (driver, None if keep_guard else watchdog, caffeine):
            if child:
                for stream_name in ('stdin', 'stdout', 'stderr'):
                    stream = getattr(child, stream_name, None)
                    if stream and not stream.closed:
                        stream.close()
        game_closed = not bool(processes(GAME_EXEC)) if watchdog is not None else None
        preserved=bool(retained and client.alive())
        workers_closed=driver is not None and driver.poll() is not None
        if preserved:
            emit('本局 AI 后台已回收，炉石继续保持运行',game_preserved=True,
                 game_pid=client.pid,match_workers_closed=workers_closed)
        elif game_closed is False and keep_guard:
            emit('AI 已停止，当前炉石及其资源保护保持运行', game_closed=False, guard_only=True, game_pid=client.pid)
        elif game_closed is False:
            emit("AI 已停止，但炉石进程仍在运行，关闭尚未确认", stopped=True, game_closed=False, error=True)
        else:
            emit("已停止：子进程已回收，设置恢复结果见上方记录", stopped=True,
                 game_closed=True if watchdog is not None else None)
        try:
            if dest and not stopped_before_queue:
                from match_review import write_review
                review_summary = write_review(dest, DATA / "learning", game_closed=game_closed,
                    game_preserved=preserved, match_workers_closed=workers_closed,
                    game_pid=client.pid if client else None)
                if review_summary.get('version_stats'):
                    from version_stats import display
                    emit(('标准天梯 · ' if mode=='ranked' else '人机练习 · ')+display(review_summary['version_stats']))
                for model,usage in review_summary.get('model_token_usage',{}).items():
                    if 'output_tokens' in usage:
                        text = f"{model} · {usage['output_tokens_reports']} 份用量回执共 {usage['output_tokens']} 个输出 token"
                        if usage.get('reasoning_tokens_reports')==usage['output_tokens_reports']:
                            text += f"，其中推理 {usage['reasoning_tokens']}"
                        emit(text)
                if preserved:client.remember(dest,review_summary)
                emit("对局复盘已保存：%s；%s" % (review_summary["outcome"],
                     "下次开局前需复盘" if review_summary["review_before_next_game"] else "已记录决策和资源指标"),
                     review_outcome=review_summary["outcome"])
        except (OSError, ValueError):
            emit("复盘报告未能保存；原始有界记录仍在本次运行目录中", error=True)
        finally:
            if lock:lock.close()
            if _event_log:
                _event_log.close()
                _event_log = None
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    if review_summary is None:
        raise SessionError("缺少完整复盘记录；不继续下一局")
    return review_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    config = validate_config(json.loads(sys.stdin.readline(4097)))
    if config.get('mode') == 'ranked_auto':
        from ranked_autoplay import run_autoplay
        run_autoplay(config, args.parent_pid, DATA, run, emit, client_factory=PersistentClient)
        return
    from evaluation_session import consume_request, run_evaluation
    suite = consume_request(DATA)
    if suite:
        run_evaluation(config, ROOT, DATA, args.parent_pid, emit, stop_child, suite)
    else:
        from game_request import consume_game_request
        mode = consume_game_request(DATA)
        if mode in ("ranked_batch", "ranked_auto"):
            from ranked_autoplay import run_autoplay
            run_autoplay(config, args.parent_pid, DATA, run, emit, client_factory=PersistentClient)
        else:
            run(config, args.parent_pid, mode if mode != 'practice' else config.get('mode', mode))


if __name__ == "__main__":
    try:
        main()
    except (SessionError, SupervisorError, OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        emit(str(error), error=True)
        sys.exit(2)
    finally:
        if _event_log:
            _event_log.close()
