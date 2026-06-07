# Changelog

## v1.0.0 - 2026-06-07

### 新增

- 新增 iOS 液态玻璃视觉风格：
  - 半透明玻璃面板
  - 边缘高光
  - 背景高斯模糊
  - 背景饱和度增强
- 新增当前会话 LLM 状态显示，可联动 `astrbot_plugin_llm_session_acl`。
- 新增当前会话模型和人格显示，读取方式对齐 AstrBot 官方 `builtin_commands`。
- 新增当前会话 token 统计。
- 新增最近 24 小时消息曲线。
- 新增最近一天会话 token 排名。
- 新增最近一天模型 token 堆叠柱状图和模型调用排名。
- 新增从数据库读取禁用插件列表并计算已启用插件数。

### 调整

- 移除运行时间显示，避免错误数据误导。
- 移除 WebUI 统计概览中的程序 CPU 占用块，替换为当前会话 token。
- 移除消息总数概览块，消息总量改在消息曲线面板中展示。
- 将模型调用区域移动到底部，并改为图表加文字排行布局。
- 将 AstrBot 版本依赖调整为 `>=4.16`。

### 数据来源

- 消息统计：AstrBot Dashboard/base stats。
- 模型统计：`ProviderStat`。
- 会话 token：当前会话 `conversation_id` 对应的 `ProviderStat` 聚合。
- 插件数量：`Preference(scope='global', key='inactivated_plugins')` 与当前插件注册表。
