# 变更日志（CHANGELOG）

本项目为 n8n「飞书群机器人存档」工作流的版本化记录。按时间倒序。

## [2026-10-30] 安全脱敏 + GitHub 同步基建 / 配图增强

### 新增
- **GitHub 仓库基建**：`.gitignore`、`.env.example`、`README.md`、`docs/TUTORIAL.md`（节点级教程）
- **密钥脱敏机制**：`scripts/sanitize_workflow.py` 把运行版工作流转为占位符版，保证密钥不入 Git 历史
- **每日自动同步**：`automation/daily_sync.ps1` + `install_task.ps1`（Windows 计划任务，每日 23:30，有变更才推送）

### 变更
- **简报字数 3000 → 2000**：AI Agent 提示词每节下限 450 → 300 字；Ollama `numPredict` 6144 → 4096
- **配图链路**：SearXNG `categories=images` 按 AI 关键词取图（规划：每关键词 3 张、卡片集中显示）

### 修复
- **飞书文档"无权限"（根因=双 App 混乱）**：
  - `飞书获取Token` 硬编码旧 App `cli_aa35ca...`（显示名"n8n测试连接"），而回发链路用新 App → 文档 owner 属旧 App，跨 App 无权限
  - 统一为同一新 App；`设置文档权限` 放宽为 `anyone_readable` + `external_access:true`
  - 历史 10 个文档用旧 App 凭据批量补权限（10/10 成功）

## [更早] 基础能力
- 群 `@机器人` 触发 → 搜索 → AI 简报 → 邮件 / WordPress / 飞书文档 / 多维表格归档 → 群内回发图文卡片
- `url_verification` 握手、`im.message.receive_v1` 事件订阅
