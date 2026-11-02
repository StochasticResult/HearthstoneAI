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

## 代码入口

- `native/App.swift`：原生应用、钥匙串和用户控制。
- `runtime/session.py`：单实例会话、启动顺序、日志保留和清理。
- `runtime/game_watchdog.py`：游戏资源采样、退出和 FPS 恢复。
- `runtime/resource_guard.py`：AI 子进程组限制与父进程退出回收。
- `runtime/state-bridge/practice_driver.py`：局面、动作、确认状态机。
- `runtime/state-bridge/openai_advisor.py`：公开状态投影、动作菜单、API、预算。
- `runtime/state-bridge/incremental_board.py`：有界增量日志状态。

辅助程序只针对炉石的进程和窗口；禁止全局应用切换循环、盲点其他程序或绕过未通过的状态校验。保持用户禁止购买的约束。不要为了让测试继续而静默放宽资源上限。

启动通过固定的 `Battle.net --exec=launch WTCG` 请求，让战网负责登录会话；使用这一启动方式的上游参考是 [HearthSim 的 HearthstoneRunner](https://github.com/HearthSim/Hearthstone-Deck-Tracker/blob/master/Hearthstone%20Deck%20Tracker/Utility/HearthstoneRunner.cs)。命令退出成功只表示已发请求，必须等守护器确认游戏进程、Mirror 确认已知场景加载后才可聚焦和操作。Unity 创建窗口前可能已能附加进程，但读到空场景，此时必须等待。

每次运行的 `session-events.jsonl` 记录资源采样、场景转换、守护停止原因和 FPS 恢复结果，上限 1 MiB；不要向此日志传递 API 配置或原始账号字段。即使守护器退出，也必须先排空 stdout 的尾部事件，以免丢失失败原因。
