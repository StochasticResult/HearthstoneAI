"""Continuous guarded Standard play; reviews never interrupt an active game."""
import json
import os
import signal
import time

from match_review import save


class AutoplayCancelled(RuntimeError):
    pass


def stop_reason(review):
    if not isinstance(review, dict) or review.get('outcome') not in ('WON', 'LOST', 'TIED'):
        return 'incomplete_game'
    if review.get('game_type') != 'GT_RANKED':
        return 'ranked_result_not_verified'
    if review.get('game_closed') is not True:
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


def run_autoplay(config, parent_pid, data, run_one, emit, pause=time.sleep):
    learning = data / 'learning'
    learning.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = learning / 'autoplay-state.json'
    pause_path = learning / 'pause-after-game.json'
    state = {'status': 'running', 'pid': os.getpid(), 'started_at': time.time(),
             'autoplay_id': str(time.time_ns()), 'runs': [], 'completed_games': 0,
             'last_reviewed_game': 0, 'review_due': False, 'stop_reason': None}
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def cancel(_signal, _frame):
        raise AutoplayCancelled('已收到停止指令')

    for sig in previous_handlers:
        signal.signal(sig, cancel)

    def persist():
        state['updated_at'] = time.time()
        save(path, state)

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
        persist()
        emit('开始自动天梯：持续运行；正常复盘不打断对局，无局数或时间配额')
        while True:
            if parent_pid and os.getppid() != parent_pid:
                raise AutoplayCancelled('应用已退出，停止连续对局')
            if deferred_stop():
                return state
            state['current_game'] = state['completed_games'] + 1
            persist()
            emit('自动天梯：第 %d 局' % state['current_game'], play_status='自动天梯 · 第 %d 局' % state['current_game'])
            # The native parent keeps the key only in memory. Every match
            # receives a copy and owns fresh resource monitors and log files.
            review = run_one(dict(config), parent_pid, 'ranked')
            state['runs'] = (state['runs'] + [review['run']])[-16:]
            state['completed_games'] += 1
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
            pause(10)
    except Exception as error:
        cancelled = isinstance(error, AutoplayCancelled) or str(error) in ('已收到停止指令', '应用已退出，停止本次运行')
        state.update(status='cancelled' if cancelled else 'needs_review',
                     stop_reason='user_or_app_stop' if cancelled else 'session_error',
                     detail=str(error)[:240])
        persist()
        raise
    finally:
        config['api_key'] = ''
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
