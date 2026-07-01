# AstrBot Status Card

生成一张 iOS 液态玻璃风格的 AstrBot 状态卡片。发送 `/状态` 或 `/status` 后，插件会展示机器人状态、当前会话信息、消息趋势、会话 token 排名和模型调用统计。

## 功能

- 机器人信息：平台头像、当前模型、当前人格、好友数、群聊数。
- 顶部统计：进程内存、平台实例数、今日模型 token、当前会话 token。
- 消息概览：消息总数和最近 24 小时平滑消息曲线。
- 会话 token 排名：最近一天会话 token Top 10。
- 模型调用：最近一天模型 token 堆叠柱状图、调用次数、成功率。
- 模型调用排名：按模型展示 token、调用次数、输入、输出、缓存命中、命中率、平均延迟和成功率。

## 使用

1. 将插件放入 AstrBot 插件目录并启用。
2. 在聊天中发送 `/状态` 或 `/status`。

无法读取的数据会显示为 `-`，不会使用假数据填充。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `display_title` | string | `YUKI STATUS` | 状态卡标题 |
| `fallback_bot_name` | string | `AstrBot` | 无法读取机器人昵称时使用 |
| `show_model_stats` | bool | `true` | 是否显示模型调用和会话 token 排名 |
| `show_message_stats` | bool | `true` | 是否显示消息概览 |

隐藏兼容配置：

- `network_sample_interval_seconds`
- `network_window_minutes`

## 数据来源

- AstrBot 平台适配器：机器人头像、好友数、群聊数。
- AstrBot Dashboard/base stats：消息数量、平台统计、消息时间序列。
- `ProviderStat`：模型 token、调用次数、成功率、TTFT、响应时间、模型排行、会话 token 排名。
