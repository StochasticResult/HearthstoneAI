"""Continuous guarded Standard play; reviews never interrupt an active game."""
import json
import fcntl
import os
import signal
import subprocess
import time

from match_review import save
from resource_guard import swap_used, MIB
from maintenance import cleanup, readiness
from navigation import stop_child


class AutoplayCancelled(RuntimeError):
    pass


def stop_reason(review):
    if not isinstance(review, dict) or review.get('outcome') not in ('WON', 'LOST', 'TIED'):
        return 'incomplete_game'
    if review.get('game_type') != 'GT_RANKED':
        return 'ranked_result_not_verified'
    retained = (review.get('game_preserved') is True and review.get('match_workers_closed') is True
                and type(review.get('game_pid')) is int and review['game_pid'] > 0)
    if review.get('game_closed') is not True and not retained:
        return 'game_cleanup_not_verified'
    if review.get('unconfirmed_decisions'):
        return 'unconfirmed_action'
    if review.get('started_without_mulligan') or not review.get('mulligan_confirmed'):
        return 'midgame_start_requires_review'
    if review.get('consecutive_losses', 0) >= 3:
        return 'three_consecutive_losses'
    return None


def read_small(path):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 2048:
        raise RuntimeError('Invalid local review request')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError('Invalid local review request')
    return value


def run_autoplay(config, parent_pid, data, run_one, emit, pause=time.sleep, client_factory=None):
    learning = data / 'learning'
    learning.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = learning / 'autoplay-state.json'
    pause_path = learning / 'pause-after-game.json'
    # Cover maintenance/cooldown as well as matches. A second coordinator
    # must not launch a game while the first is waiting for resources.
    lock = (data / 'autoplay.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        config['api_key'] = ''
        raise RuntimeError('已有自动天梯程序在运行或等待资源') from None
    state = {'status': 'running', 'pid': os.getpid(), 'started_at': time.time(),
             'autoplay_id': str(time.time_ns()), 'runs': [], 'completed_games': 0,
             'last_reviewed_game': 0, 'review_due': False, 'stop_reason': None}
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    awake = None
    client = None

    def cancel(_signal, _frame):
        raise AutoplayCancelled('已收到停止指令')

    for sig in previous_handlers:
        signal.signal(sig, cancel)

    def persist():
        state['updated_at'] = time.time()
        save(path, state)

    def check_parent():
        if parent_pid and os.getppid() != parent_pid:
            raise AutoplayCancelled('应用已退出，停止连续对局')

    def maintain(baseline):
        # Called only outside run_one. The single-game supervisor has already
        # closed its children and descriptors before control returns here.
        state.update(status='maintenance', maintenance=cleanup(data))
        persist()
        previous_reasons = None
        while True:
            check_parent()
            if deferred_stop():
                return False
            if client:client.tick()
            sample = readiness(baseline)
            state['resources'] = sample
            if sample['ready']:
                state.update(status='running', stop_reason=None)
                persist()
                if previous_reasons is not None:
                    emit('局间资源已恢复，程序自动继续排队', play_status='资源已恢复 · 继续自动天梯')
                return True
            state.update(status='waiting_for_resources', stop_reason=None)
            persist()
            reasons = tuple(sample['reasons'])
            if reasons != previous_reasons:
                emit('已完成局间清理，等待资源恢复：' + '；'.join(reasons),
                     play_status='等待资源恢复 · 将自动继续', resources=sample)
                previous_reasons = reasons
            # No game, screenshot, focus change, API call or launch retry.
            # SIGTERM is immediate; the deferred-stop file is checked at most
            # 15 seconds later. The process remains idle between samples.
            pause(15)

    def deferred_stop():
        request = read_small(pause_path)
        if not request:
            return False
        pause_path.unlink()
        user_stop = request.get('reason') == 'user_requested_stop'
        state.update(status='cancelled' if user_stop else 'paused_for_review',
                     stop_reason='user_after_game_stop' if user_stop else 'review_requested')
        persist()
        emit('本局已结束，已停止继续排队', play_status='已完成本局 · 停止排队')
        return True

    try:
        # Keep the already-authorized awake assertion through resource waits
        # and cooldowns too. It expires with this coordinator, never persists
        # as a changed system setting or unlocks a locked Mac.
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        swap_baseline = swap_used()
        if client_factory:
            client = client_factory(parent_pid, swap_baseline)
            state['persistent_client'] = True
        state['swap_baseline_mib'] = round(swap_baseline / MIB, 1)
        persist()
        emit('开始自动天梯：持续运行；正常复盘不打断对局，无局数或时间配额')
        while True:
            check_parent()
            if deferred_stop():
                return state
            if not maintain(swap_baseline):
                return state
            state['current_game'] = state['completed_games'] + 1
            persist()
            emit('自动天梯：第 %d 局' % state['current_game'], play_status='自动天梯 · 第 %d 局' % state['current_game'])
            # The native parent keeps the key only in memory. Every match
            # receives a copy and owns fresh resource monitors and log files.
            # Keep one baseline across match cleanup: accumulated growth must
            # not disappear merely because a new game starts.
            kwargs={'swap_baseline':swap_baseline}
            if client:kwargs['client']=client
            review = run_one(dict(config), parent_pid, 'ranked', **kwargs)
            state['runs'] = (state['runs'] + [review['run']])[-16:]
            if review.get('outcome') in ('WON','LOST','TIED'):
                state['completed_games'] += 1
            if client:state['game_pid']=client.pid
            state['stop_reason'] = stop_reason(review)
            if state['stop_reason']:
                state['status'] = 'needs_review'
                persist()
                emit('本局结束后暂停复盘：' + state['stop_reason'], play_status='本局已结束 · 等待问题复盘')
                return state
            ack = read_small(learning / 'review-ack.json') or {}
            if ack.get('autoplay_id') == state['autoplay_id']:
                count = ack.get('completed_games')
                if type(count) is int and 0 <= count <= state['completed_games']:
                    state['last_reviewed_game'] = count
            state['review_due'] = state['completed_games'] - state['last_reviewed_game'] >= 3
            persist()
            if deferred_stop():
                return state
            # Process/FPS cleanup is complete; a short cooldown is not a
            # session cap, and a user stop remains effective during it.
            pause(2 if client else 10)
    except Exception as error:
        cancelled = isinstance(error, AutoplayCancelled) or str(error) in ('已收到停止指令', '应用已退出，停止本次运行')
        state.update(status='cancelled' if cancelled else 'needs_review',
                     stop_reason='user_or_app_stop' if cancelled else 'session_error',
                     detail=str(error)[:240])
        persist()
        raise
    finally:
        config['api_key'] = ''
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            try:
                if client:client.close()
            finally:
                stop_child(awake)
        finally:
            lock.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
