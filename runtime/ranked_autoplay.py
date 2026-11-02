"""Continuous guarded Standard play; reviews never interrupt an active game."""
import json
import fcntl
import os
import signal
import subprocess
import time
import math

from match_review import save
from resource_guard import swap_used, MIB
from maintenance import cleanup, readiness, RESOURCE_CHECK_SECONDS, WAIT_LABELS
from navigation import stop_child
from app_awake import AppWake


class AutoplayCancelled(RuntimeError):
    pass


class AfterGameStop(RuntimeError):
    """A deferred stop arrived while an already-finished game was settling."""
    pass


def reviewed_within_loss_streak(review, boundary, path):
    if not boundary or not path or not path.is_file() or path.is_symlink():
        return False
    try:
        if path.stat().st_size > 256 * 1024:
            return False
        history = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return False
    if history[-1].get('run') != review.get('run'):
        return False
    losses = 0
    for row in reversed(history):
        if not isinstance(row, dict) or row.get('game_type') != 'GT_RANKED':
            break
        if row.get('outcome') == 'INCOMPLETE':
            continue
        if row.get('outcome') != 'LOST':
            break
        if row.get('run') == boundary:
            return True
        losses += 1
        if losses == 3:
            break
    return False


def stop_reason(review, *, reviewed_loss_run=None, history_path=None):
    if not isinstance(review, dict) or review.get('outcome') not in ('WON', 'LOST', 'TIED'):
        return 'incomplete_game'
    if review.get('game_type') != 'GT_RANKED':
        return 'ranked_result_not_verified'
    if review.get('result_after_ai_fault'):
        return 'ai_fault_during_match'
    retained = (review.get('game_preserved') is True and review.get('match_workers_closed') is True
                and type(review.get('game_pid')) is int and review['game_pid'] > 0)
    if review.get('game_closed') is not True and not retained:
        return 'game_cleanup_not_verified'
    if review.get('unconfirmed_decisions'):
        return 'unconfirmed_action'
    if review.get('started_without_mulligan') or not review.get('mulligan_confirmed'):
        return 'midgame_start_requires_review'
    if review.get('consecutive_losses', 0) >= 3 and not reviewed_within_loss_streak(review, reviewed_loss_run, history_path):
        return 'three_consecutive_losses'
    return None


def read_small(path, limit=2048):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > limit:
        raise RuntimeError('Invalid local review request')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError('Invalid local review request')
    return value


def reviewed_streak_boundary(prior, ack):
    runs = prior.get('runs') or []
    if (prior.get('status') == 'needs_review' and prior.get('stop_reason') == 'three_consecutive_losses'
            and prior.get('autoplay_id') and ack.get('autoplay_id') == prior['autoplay_id']
            and type(ack.get('completed_games')) is int and 0 < ack['completed_games'] == prior.get('completed_games')
            and runs and isinstance(runs[-1], str)):
        return runs[-1]
    return prior.get('reviewed_loss_run')


def continued_swap_baseline(path, current):
    """A review/update restart must not hide already accumulated swap growth."""
    if not path.exists():
        return current
    if path.is_symlink() or path.stat().st_size>16384:
        raise RuntimeError('Invalid previous autoplay state')
    prior=json.loads(path.read_text())
    if not isinstance(prior,dict):
        raise RuntimeError('Invalid previous autoplay state')
    if prior.get('status') not in ('paused_for_review','needs_review','running','maintenance','waiting_for_resources','holding_match_for_review'):
        return current
    previous=prior.get('swap_baseline_mib')
    if type(previous) not in (int,float) or not math.isfinite(previous) or previous<0:
        raise RuntimeError('Missing previous continuation resource baseline')
    return min(current,round(previous*MIB))


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
    # Read before persist() replaces the previous run. Explicit user stops
    # begin a new session; planned review/fault recovery continues its baseline.
    try:
        observed_swap=swap_used()
        swap_baseline=continued_swap_baseline(path,observed_swap)
        reviewed_loss_run = reviewed_streak_boundary(read_small(path, 16384) or {},
                                                     read_small(learning / 'review-ack.json') or {})
    except Exception:
        lock.close()
        config['api_key']=''
        raise
    state = {'status': 'running', 'pid': os.getpid(), 'started_at': time.time(),
             'autoplay_id': str(time.time_ns()), 'runs': [], 'completed_games': 0,
             'last_reviewed_game': 0, 'review_due': False, 'stop_reason': None,
             'reviewed_loss_run': reviewed_loss_run}
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    awake = None
    app_wake = None
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
        previous_sample = None
        while True:
            check_parent()
            if deferred_stop():
                return False
            if client:client.tick()
            sample = readiness(baseline, previous=previous_sample)
            previous_sample = sample
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
                explanation = '、'.join(WAIT_LABELS.get(reason, reason) for reason in reasons)
                emit('局间等待 · ' + explanation,
                     play_status=explanation + ' · 将自动续排', resources=sample)
                previous_reasons = reasons
            # No game, screenshot, focus change, API call or launch retry.
            # SIGTERM is immediate; the deferred-stop file is checked at most
            # 15 seconds later. The process remains idle between samples.
            pause(RESOURCE_CHECK_SECONDS)

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
        if parent_pid:
            check_parent()
            app_wake = AppWake.acquire(data, parent_pid)
            state['keep_awake'] = {'app_pid': parent_pid, 'holder_pid': app_wake.pid,
                                   'policy': 'on_ac_through_review'}
            emit('保持唤醒已启用 · 接通电源时覆盖对局、等待和复盘，手动停止或退出应用时释放')
        else:
            # A standalone invocation has no GUI owner beyond this process.
            awake = subprocess.Popen(['/usr/bin/caffeinate', '-di', '-w', str(os.getpid())],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from build_identity import current_build
        from version_stats import status as version_status, display
        build=current_build()
        state['runtime_version']=build['version_id']
        state['version_stats']=version_status(learning,build['version_id'])
        emit('标准天梯 · '+display(state['version_stats']))
        if client_factory:
            client = client_factory(parent_pid, swap_baseline)
            state['persistent_client'] = True
            def mark_match_held():
                state.update(status='holding_match_for_review', stop_reason='ai_fault_during_match',
                             game_pid=client.pid)
                persist()
            client.on_match_hold = mark_match_held
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
            if review.get('version_stats'):
                state['version_stats']=review['version_stats']
            state['stop_reason'] = stop_reason(review, reviewed_loss_run=reviewed_loss_run,
                                               history_path=learning / 'match-history.json')
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
    except AfterGameStop:
        if not deferred_stop():
            state.update(status='paused_for_review',stop_reason='postgame_stop')
            persist()
        return state
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
                try:
                    if app_wake is not None:
                        # A request can arrive while the single-game driver is
                        # unwinding a fault. An explicit stop takes precedence.
                        request = read_small(pause_path)
                        if request and request.get('reason') == 'user_requested_stop':
                            deferred_stop()
                        if state['status'] in ('needs_review', 'paused_for_review'):
                            emit('复盘期间保持唤醒 · 退出应用可结束')
                        else:
                            app_wake.release()
                finally:
                    stop_child(awake)
        finally:
            lock.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
