"""Explicit one-use ranked test request for the existing signed app launcher."""
import json
import time


def consume_game_request(data):
    path = data / "game-request.json"
    if not path.exists():
        return "practice"
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError("游戏模式请求无效；未启动游戏")
    request = json.loads(path.read_text())
    path.unlink()
    if (set(request) != {"mode", "expires_at"} or request["mode"] not in ("ranked", "ranked_batch", "ranked_auto")
            or type(request["expires_at"]) not in (int, float)
            or not 0 <= request["expires_at"] - time.time() <= 300):
        raise ValueError("天梯测试请求已过期或无效；未启动游戏")
    return request["mode"]
