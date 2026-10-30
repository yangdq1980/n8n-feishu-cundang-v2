# 变更日志（CHANGELOG）

本项目为 n8n「飞书群机器人存档」工作流的版本化记录。按时间倒序。

## [2026-10-30 晚] 多图改造（每主题 3 张 × 2 = 6 张）+ 多维表格迁移 + 防幻觉加固

### 新增
- **节点 `拆分配图`**（Code, id `a1b2c3d4-0010`）：把 Code3 的 `imageUrls` 拆成 N 个 item，
  让后续 HTTP 节点逐张下载/上传。节点数 **34 → 35**。
- **`docs/TUTORIAL.md`**：35 节点逐节深度教程（13000+ 字，含参数逐项、上下游契约、机理、踩坑、代码注释）。
- **`scripts/patch_multi_images.py`**：多图改造的幂等补丁脚本（按节点 id 定位，可重复执行）。
- **`_diag/test_multiimg.js`**：多图链路 mock 单测（40 项断言，不依赖 n8n，`node` 直接跑）。

### 变更（配图链路，核心）
- **Code1**：关键词由「只取 LLM 第 1 个」改为「**用户主题固定为第 1 个 + LLM 细分词第 2 个**」，上限 2 个。
  - 目的：防止 LLM 在检索素材为空时幻觉出无关关键词（实测案例：问「低空经济」→ 生成「新能源汽车」）。
- **Code2**：`Run Once for All Items` 逐条处理每个关键词；每关键词挑 **6 张候选**（原 3 张），
  含扩展名白名单、`svg/ico/tiff` 黑名单、强防盗链域名剔除（`toutiaoimg/pstatp/byteimg`）。
- **Code3**：配额改**两轮分配** —— 第一轮每词 3 张（保证"每主题 3 张"），
  第二轮在总量不足 6 张时用其他关键词的剩余候选补足（解决"某词 0 图只有 3 张"）。
  新增输出 `imageUrls[]` / `imageCount` / `keywords[]`。
- **`组装群聊回发卡`**：由"只发 1 个 image_key"改为**收集全部上传成功的 key，第 1 条消息正文后集中展示**（一图一行）。
- **`回发下载配图`**：URL 改 `$json.url`（逐 item）；加 20s 超时、2 次重试、`allowUnauthorizedCerts`（证书链不全的图床）。
- **`HTTP Request` / `HTTP Request1`**：加 20s 超时 + 2 次重试；图片 `num_results` 5 → 8。

### 修复
- **多维表格长期静默 403**：原指向旧 App 创建的 Base（`DcIwb...`），当前 App 无权限（`code 91403`）。
  - 实测飞书 `permissions/{token}/members` 对**应用类型协作者**支持极差（`openapp` → field validation failed，`appid` → Invalid parameter），无法 API 加协作者。
  - **解法**：用机器人 App **自建** Base（`飞书存档 · 搜索记录`），建 6 个文本字段，改指向新表。实测 `code 0, created 7`。
  - `组装搜索存档` 同步改为兼容 `imageUrls`（原读 `c3.images`，多图改造后会漏写配图）。
- **配图 "0 张" 的连锁故障**：searxng 返回 0 条 → `search_text` 为空 → LLM 幻觉跑题。
  - `07 Code` 新增 `noMaterial` 标记 + 明确提示词兜底（"仅依据主题通识作答，禁止编造数据"）。
- **SSL 证书不全导致下载失败**：`NodeSslError` → 仅对图片下载节点开启 `allowUnauthorizedCerts`。
- **防盗链 403**：实测"加 Referer"会**破坏新浪图床**（它要求空 Referer），故不加自定义头，改为上游剔除必 403 域名。

### 验证
- mock 单测 40 项断言全通过；
- 7 轮真实 E2E（execId 6272/6273/6274/6276/6277/6278/6279），最终 execId **6279**：
  - 关键词 `["人形机器人产业化进展与成本曲线","人形机器人产业化商业化应用"]`（主题相关）
  - 6 张候选 → **6 张下载成功 → 6 张上传成功 → 卡片 6 个 img 块 → 发送 code=0**
  - 多维表格 `created=7`（文字 1 + 图片 6）

---

## [2026-10-30] 安全脱敏 + GitHub 同步基建 / 配图增强

### 新增
- **GitHub 仓库**：https://github.com/yangdq1980/n8n-feishu-cundang-v2
- **仓库基建**：`.gitignore`、`.env.example`、`README.md`
- **密钥脱敏机制**：`scripts/sanitize_workflow.py` 把运行版工作流转为占位符版，保证密钥不入 Git 历史
  （从 `.env` 读真实值，脚本自身零硬编码；输出后做残留扫描）
- **每日自动同步**：`automation/daily_sync.sh`（Bash）+ `install_task.ps1`（Windows 计划任务，每日 23:30，**有变更才推**）+ `push_now.bat`

> 备注：最初用 PowerShell 版同步脚本，因 PS 5.1 在本仓库反复出现 `index.lock` 与中文编码问题，改用 Bash 版并让计划任务调用 Git-Bash，已实测通过。

### 变更
- **简报字数 3000 → 2000**：AI Agent 提示词每节下限 450 → 300 字；Ollama `numPredict` 6144 → 4096
- **配图链路**：SearXNG `categories=images` 按 AI 关键词取图

### 修复
- **飞书文档"无权限"（根因=双 App 混乱）**：
  - `飞书获取Token` 硬编码旧 App `cli_aa35ca...`（显示名"n8n测试连接"），而回发链路用新 App → 文档 owner 属旧 App，跨 App 无权限
  - 统一为同一新 App；`设置文档权限` 放宽为 `anyone_readable` + `external_access:true`
  - 历史 10 个文档用旧 App 凭据批量补权限（10/10 成功）
- **计划任务"假成功"**：任务返回 0 但未实际执行 → 改用 Bash 脚本 + 绝对路径 + 显式退出码检查

## [更早] 基础能力
- 群 `@机器人` 触发 → 搜索 → AI 简报 → 邮件 / WordPress / 飞书文档 / 多维表格归档 → 群内回发图文卡片
- `url_verification` 握手、`im.message.receive_v1` 事件订阅
- 先回执（Respond 回执）再执行重活，规避飞书 3s 事件超时重推
