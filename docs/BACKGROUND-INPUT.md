# 后台输入验证 · 2026-11-01

结论：当前生产应用仍要求炉石在前台，使用全局 HID 鼠标事件。未实现用户同时使用鼠标、炉石在后台独立对局。下面的失败不能证明任何后台方案都不可能，但不能据此启用自动对局。

## 实测

在独立游戏守护下启动炉石，使用主菜单「传统对战」作为无购买、无匹配副作用的目标。把已有 Hearthstone AI 控制窗口置前，不隐藏或最小化炉石。用 HearthMirror 检查菜单场景是否从 3 进入 7。

| 输入方式 | 鼠标/前台测量 | 游戏结果 |
|---|---|---|
| 私有 CGEventSource，CGEventPostToPid，只发给炉石 PID | 前后鼠标坐标和前台 PID 一致 | 场景仍为 3 |
| 带目标 windowNumber 的 NSEvent 转 CGEvent，再 PostToPid | 前后鼠标坐标和前台 PID 一致 | 场景仍为 3 |
| 现有电脑工具的窗口坐标点击 | 操作后前台仍为控制应用，鼠标位置不变 | 场景仍为 3 |

未测试拖牌，也未测试最小化窗口。没有发送游戏对局动作或调用决策 API。没有修改游戏代码、注入游戏进程或更改系统输入权限。两个本地事件发送实现保存在 `native/BackgroundInputProbe.m`，只作为开发探针，未打包/安装为生产输入后端。

这次菜单检查约 170 秒，游戏采样内存峰值 1097.8 MiB，热状态始终 0，交换空间未增加；退出后恢复 60 FPS，炉石进程已回收。不是 GPU 温度或功耗测量。

## 接口依据和限制

- [Apple CGEvent / postToPid](https://developer.apple.com/documentation/coregraphics/cgevent)：存在向特定进程投递事件的公开接口，不保证第三方游戏会按目标坐标处理。
- [Unity 官方后台输入说明](https://github.com/Unity-Technologies/InputSystem/blob/develop/Packages/com.unity.inputsystem/Documentation~/background-behavior.md)：后台运行和焦点丢失后的输入处理是分别配置的。它解释了一类可能原因，但尚未核实本机炉石实际使用的输入路径，不能把它当作已定位的根因。

下一次改动必须先证明菜单点击与悬停有效，再验证人机拖牌及攻击；同时测量鼠标和前台窗口不被改变。不能只凭事件 API 返回成功、也不能只把真实光标移回原处，就宣称支持互不干扰的后台操作。
