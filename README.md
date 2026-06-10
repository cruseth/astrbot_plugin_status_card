# AstrBot Status Card

生成一张 iOS 液态玻璃风格的 AstrBot 状态卡片。发送 `/状态` 或 `/status` 后，插件会渲染机器人身份、系统性能、WebUI 统计、当前会话模型与人格、会话 token、消息趋势和模型调用统计。

## 功能

- 机器人信息：昵称、账号 ID、平台状态、好友数、群聊数、头像。
- 系统性能：CPU、内存、进程内存。
- WebUI 统计概览：平台实例数、今日模型 token、当前会话 token、已启用插件数。
- 当前会话信息：
  - 从 AstrBot 当前会话配置读取正在使用的 LLM 模型。
  - 从 AstrBot 官方会话/人格逻辑读取当前生效人格。
- 消息概览：最近 24 小时消息曲线和消息总量。
- 会话 token 排名：最近一天会话 token 排名。
- 模型调用：最近一天模型 token 堆叠柱状图、模型图例、模型调用排名。
- 插件数量：从 AstrBot 数据库偏好项读取禁用插件列表，计算当前已启用插件数。
- 视觉：液态玻璃面板、高斯模糊背景、增强饱和度、玻璃边缘高光。

无法真实读取的数据会显示为 `-`，不会用假数据填充。

## 使用

1. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 将插件放入 AstrBot 插件目录并启用。

3. 发送：

   ```text
   /状态
   /status
   ```

## 当前会话信息

当前会话模型和人格读取逻辑参考 AstrBot 官方 `builtin_commands`：

- 模型：`context.get_using_provider(umo).meta()`
- 人格：`conversation_manager` + `persona_manager.resolve_selected_persona(...)`

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `display_title` | string | `YUKI STATUS` | 状态卡左上角标题 |
| `fallback_bot_name` | string | `AstrBot` | 无法从平台读取机器人昵称时使用 |
| `avatar_file` | file | 空 | 平台头像不可用时使用的默认头像 |
| `background_file` | file | 空 | 自定义背景图，会自动应用高斯模糊和高饱和玻璃背景 |
| `show_model_stats` | bool | `true` | 是否显示模型调用和会话 token 排名 |
| `show_message_stats` | bool | `true` | 是否显示消息概览 |

隐藏配置项：

- `network_sample_interval_seconds`
- `network_window_minutes`

这两个配置保留用于兼容旧版本，不影响当前默认布局。

## 数据来源

- `PlatformStat` / Dashboard base stats：消息数量、平台统计、消息时间序列。
- `ProviderStat`：模型 token、调用次数、成功率、TTFT、响应时间、模型排行、会话 token 排名。
- `Preference`：禁用插件列表，用于计算当前已启用插件数。
- 平台适配器：机器人登录信息、好友列表、群列表。

