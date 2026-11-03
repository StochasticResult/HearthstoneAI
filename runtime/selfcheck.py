"""Offline smoke check. No network, game launch or input events."""
import json
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "state-bridge"))
from openai_advisor import OpenAIAdvisor, action_menu, public_view
from incremental_board import IncrementalBoard
from input_command import input_command


def main():
    state = {"game_type": "GT_VS_AI", "format": "FT_WILD", "own_turn": True, "options_id": 1,
             "mana": 2, "game_turn": "3", "my_hero": {"id": 1, "health": 30, "armor": 0},
             "enemy_hero": {"id": 2, "health": 2, "armor": 0}, "my_hand": [],
             "my_board": [{"id": 3, "card_id": "CORE_UNG_205", "position": 1, "attack": 2, "health": 1}],
             "enemy_board": [], "end_turn_allowed": True,
             "legal_options": [{"id": 3, "type": "ATTACK", "targets": [2], "requires_target": True}]}
    def fake_api(payload):
        assert payload["store"] is False and payload["parallel_tool_calls"] is False
        return {"status": "completed", "model": payload["model"], "output": [{"type": "function_call", "name": "choose_action",
            "arguments": json.dumps({"action_id": "a0", "reason": "离线模拟：攻击对方英雄"})}],
            "usage": {"total_tokens": 0}}
    advisor = OpenAIAdvisor({"api_key": "offline-test-not-a-real-key"}, transport=fake_api)
    assert advisor.ask(state) == {"kind": "attack", "source_id": 3, "target_id": 2}
    assert "enemy_hand" not in public_view(state)
    assert len(action_menu(state)) == 2
    for name in ("mirror-status", "game_watchdog.py", "session.py"):
        assert (ROOT / name).is_file(), name
    access = subprocess.run(input_command("access"), capture_output=True, text=True, timeout=3)
    print(json.dumps({"message": "辅助功能权限检查：" + ("已具备事件发送权限" if access.returncode == 0 and access.stdout.strip() == "true" else "需要通过下方按钮授权")}, ensure_ascii=False))
    print(json.dumps({"message": "离线自检通过：动作约束、API 格式和程序组件正常。未连接 API、未启动炉石；这不等于实战验收通过。"}, ensure_ascii=False))
    advisor.close()


if __name__ == "__main__":
    main()
