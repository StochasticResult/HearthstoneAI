# 开发与验证

源代码在 `~/Projects/HearthstoneAI`。原始研究项目暂保留在此前的可视化工作目录，作为回放和行为证据；它不是应用依赖，旧控制器不应运行。

## 构建

```sh
cd ~/Projects/HearthstoneAI
/usr/bin/python3 runtime/resource_guard.py --log /tmp/hearthstone-ai-build.log --timeout 90 --max-rss-mib 600 --max-cpu 130 -- /usr/bin/nice -n 15 /usr/bin/python3 scripts/build_app.py
```

构建产物为 `dist/Hearthstone AI.app`；只使用已安装的 Xcode Command Line Tools，不下载依赖。应用本身为 AppKit，后台为系统独立进程。重新安装前应退出正在运行的应用，避免替换正在读取的组件。

本机首次安装后，Python 和辅助程序的后续更新使用 `python3 scripts/install_runtime.py`，安装到本用户独占的 `~/Library/Application Support/HearthstoneAI/runtime`。后台会话必须先停止。此更新不修改原生应用签名，从而保持钥匙串及辅助功能授权的应用身份；原生 UI 变动仍需重新构建并核对授权。没有外部运行目录时应用使用自身携带的组件。

构建脚本会从 `native/Mirror.m` 编译 `runtime/mirror-status`，并使用与此前经过实际读取的版本相同的 debugger entitlement 签名。它通过动态载入 HSTracker 的 HearthMirror 读取局面；应用不包含该第三方框架。重新构建后的权限与实际读取仍须独立验证。

## 测试

```sh
cd ~/Projects/HearthstoneAI
/usr/bin/python3 runtime/resource_guard.py --log /tmp/hearthstone-ai-tests.log --timeout 45 --max-rss-mib 240 --max-cpu 120 -- /usr/bin/env PYTHONPATH=runtime:runtime/state-bridge /opt/homebrew/bin/python3 -m unittest discover -s tests -q
```

测试中的进程清理检查只启动一次性的 `sleep` 子进程，不启动炉石，不发送 API 请求。实际 API 测试必须使用用户自行配置的本机密钥。不能将原始 `Power.log` 上传到 API、代码仓库或外部服务。

### 有界棋力评测

先在应用空闲时更新 runtime，然后运行 `/usr/bin/python3 scripts/request_strategy_eval.py`。它只排入一个五分钟内有效的本地测试请求，不接触密钥。接着在原生应用点「开始人机测试」：这一次会明确显示“固定局面棋力评测”，使用原生应用已经获准的钥匙串身份取得配置，仅运行八题、两版本、最多 16 次 API 调用，不启动游戏、不发送游戏输入。「离线自检」仍完全离线。过期或无效的请求会停止，不会意外启动游戏。

同一 `gpt-6.1-terra`、同一 low 推理档位、同一题目比较原提示/发现简略观察与新策略/完整观察，保留 18 秒请求超时、原请求和 token 上限，进程组限 200 MiB、总时长 300 秒。`~/Library/Application Support/HearthstoneAI/evaluation/results.jsonl` 与 `evaluation.log` 保存本轮结果；下一轮替换它们。结果中的规则题与策略偏好题要分别审查；固定题单次正确率不能表示实战胜率。不要把假响应测试的得分作为实际模型结果。

`strategy-pack.json` 的 11 类打法是可修改的策略假设，来源说明与日期随包保存。修改后检查卡牌 ID、过期行为、公开证据筛选和上下文预算，再做真实模型对比。新观察、卡牌平衡补丁和实际误判都应转为新的留出案例，而不是只调到现有八题通过。当前策略换牌尚未接入输入，仍需核对 Mirror 选中状态与游戏位置；不能把保留全手牌记为起手策略验收。

## 代码入口

- `native/App.swift`：原生应用、钥匙串和用户控制。
- `runtime/session.py`：单实例会话、启动顺序、日志保留和清理。
- `runtime/game_watchdog.py`：游戏资源采样、退出和 FPS 恢复。
- `runtime/resource_guard.py`：AI 子进程组限制与父进程退出回收。
- `runtime/state-bridge/practice_driver.py`：局面、动作、确认状态机。
- `runtime/state-bridge/openai_advisor.py`：公开状态投影、动作菜单、API、预算。
- `runtime/state-bridge/incremental_board.py`：有界增量日志状态。
- `runtime/state-bridge/strategy.py` / `strategy-pack.json`：牌组知识、候选对局和有效日期。
- `runtime/state-bridge/strategy_eval.py`：固定局面与 A/B 评测；没有游戏输入依赖。
- `runtime/evaluation_session.py`：通过原生应用现有密钥路径运行一次有界评测。

辅助程序只针对炉石的进程和窗口；禁止全局应用切换循环、盲点其他程序或绕过未通过的状态校验。保持用户禁止购买的约束。不要为了让测试继续而静默放宽资源上限。

启动通过固定的 `Battle.net --exec=launch WTCG` 请求，让战网负责登录会话；使用这一启动方式的上游参考是 [HearthSim 的 HearthstoneRunner](https://github.com/HearthSim/Hearthstone-Deck-Tracker/blob/master/Hearthstone%20Deck%20Tracker/Utility/HearthstoneRunner.cs)。命令退出成功只表示已发请求，必须等守护器确认游戏进程、Mirror 确认已知场景加载后才可聚焦和操作。Unity 创建窗口前可能已能附加进程，但读到空场景，此时必须等待。

每次运行的 `session-events.jsonl` 记录资源采样、场景转换、守护停止原因和 FPS 恢复结果，上限 1 MiB；不要向此日志传递 API 配置或原始账号字段。即使守护器退出，也必须先排空 stdout 的尾部事件，以免丢失失败原因。
