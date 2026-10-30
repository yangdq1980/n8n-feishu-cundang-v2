# n8n 飞书存档机器人 · 全节点深度教程

> 一支 35 节点的自动化流水线：**在飞书群里 @机器人 说一句话，它自己完成「搜索 → 写简报 → 配 6 张图 → 发邮件 → 发 WordPress → 建飞书文档 → 进多维表格 → 群里回发图文卡片」。**
>
> 本文按**真实执行顺序**逐节点拆解：它是什么、参数每个选项干什么、上游给什么、下游要什么、牵动哪个第三方系统、背后是什么机理、哪里容易踩坑。所有代码带中文注释。

---

## 目录

- [0. 阅读指南](#0-阅读指南)
- [1. 系统全景](#1-系统全景)
- [2. 前置准备与外部依赖](#2-前置准备与外部依赖)
- [3. 阶段一 · 事件接入（节点 01–05）](#3-阶段一--事件接入节点-0105)
- [4. 阶段二 · 联网检索（节点 06–07）](#4-阶段二--联网检索节点-0607)
- [5. 阶段三 · AI 简报生成（节点 08 / 33 / 34 / 35）](#5-阶段三--ai-简报生成节点-08--33--34--35)
- [6. 阶段四 · 结构化解析（节点 09）](#6-阶段四--结构化解析节点-09)
- [7. 阶段五 · 配图流水线（节点 10 / 31 / 32 / 11 / 12）](#7-阶段五--配图流水线节点-10--31--32--11--12)
- [8. 阶段六 · 多端发布（节点 13–16）](#8-阶段六--多端发布节点-1316)
- [9. 阶段七 · 飞书归档（节点 17–20 / 27–30）](#9-阶段七--飞书归档节点-1720--2730)
- [10. 阶段八 · 群内回发（节点 21–26）](#10-阶段八--群内回发节点-2126)
- [附录 A · 核心代码逐段解读](#附录-a--核心代码逐段解读)
- [附录 B · 故障排查手册](#附录-b--故障排查手册)
- [附录 C · 性能、成本与容量](#附录-c-性能成本与容量)
- [附录 D · 二次开发指南](#附录-d--二次开发指南)

---

## 0. 阅读指南

### 0.1 这份文档解决什么问题

n8n 的节点面板看起来很简单，但真跑起来会撞上一堆"看不出来"的问题：

- 为什么 Webhook 必须**先回执再干活**？
- 为什么 `$input.all()` 有时拿不到数据，要写 `$('节点名').all()` 兜底？
- 为什么配图明明搜到了 10 条，最后卡片里只有 1 张？
- 为什么同一个工作流里的飞书接口，有的能改文档权限、有的报 `permission denied`？
- `onError: continueRegularOutput` 到底在什么场景救命？

本文对每个节点回答六件事：

| 维度 | 说明 |
|---|---|
| **定位** | 编号、节点类型、typeVersion、节点 ID |
| **它做什么** | 一句话职责 + 在业务上的意义 |
| **参数逐项** | 每个配置项的含义、可选值、改了会怎样 |
| **上下游契约** | 上游给的 JSON 长什么样，下游期待什么 |
| **技术机理** | 协议层/框架层发生了什么 |
| **坑与经验** | 实测踩过的坑及规避方式 |

### 0.2 术语约定

| 术语 | 含义 |
|---|---|
| **item** | n8n 的数据单元，形如 `{ json: {...}, binary: {...} }`。一个节点收到 N 个 item 就执行 N 次（除非开 `executeOnce`） |
| **paired item** | n8n 为每个输出 item 记录的"它来自哪个输入 item"，用于 `$('上游节点').item` 定位 |
| **runData** | 一次执行里各节点的输入输出快照，存在 `execution_data` 表（用 flatted 压缩） |
| **tenant_access_token** | 飞书自建应用的"应用身份"凭证，2 小时有效，权限 = 应用被授予的权限 |
| **owner** | 飞书文档/多维表格的属主。**谁创建就归谁**，跨应用默认无权访问 |

### 0.3 版本基线

| 组件 | 版本/取值 |
|---|---|
| n8n | 自带 `n8n-nodes-base` + `@n8n/n8n-nodes-langchain` |
| Webhook / Code / HTTP Request | typeVersion 2 / 2 / 4.5 |
| AI Agent | typeVersion 3.1 |
| Ollama 模型 | `qwen3:8b`（temperature 0.4，numCtx 16384，numPredict 4096） |
| 工作流节点数 | 35 |

---

## 1. 系统全景

### 1.1 一句话说明

**输入**：飞书群里一条 @机器人的文字消息（任意主题）。
**输出**：7 个落地物 —— ①群内图文卡片 ②飞书云文档 ③飞书多维表格记录 ④WordPress 文章 ⑤WordPress 特色图 ⑥HTML 邮件 ⑦（可选）MCP 侧文件。

### 1.2 端到端数据流

```
                    ┌─────────────────────────────────────────────┐
   飞书群 @机器人 ──►│ 01 Webhook  ◄── 飞书开放平台事件推送（POST） │
                    └──────┬──────────────────────────────────────┘
                           ▼
                    02 飞书事件解析 ──► 03 验证握手?
                                        ├─ Y ──► 04 Respond 验证   （回 challenge，仅首次配置用）
                                        └─ N ──► 05 Respond 回执   （先 200 回执，避免飞书 3s 超时重推）
                                                    │
                                                    ▼
                                        06 HTTP Request（searxng general 检索）
                                                    ▼
                                        07 Code in JavaScript（拼 search_text）
                                                    ▼
                                        08 AI Agent ──(ai_languageModel)── 33 Ollama qwen3:8b
                                                    ├─(ai_tool)──────────── 34 MCP Client2
                                                    └─(ai_memory)────────── 35 Simple Memory
                                                    ▼
                                        09 Code in JavaScript1（解析 JSON + 拆关键词）
                                                    ▼
                                        10 Switch（按 type 分流）
                                        ├─ brief ──────────────────────────────┐
                                        └─ image_search                        │
                                              ▼                                │
                                   31 HTTP Request1（searxng images，逐关键词） │
                                              ▼                                │
                                   32 Code in JavaScript2（每词挑 3 张）        │
                                              └──────────► 11 Merge ◄──────────┘
                                                              ▼
                                                  12 Code in JavaScript3
                                                （汇总去重 + 配额 + 邮件 HTML）
                                                              ▼
                                          13 HTTP Request2（下载首图，取二进制）
                                                              ▼
                                          14 HTTP Request3（上传 WordPress 媒体库）
                                                              ▼
                                          15 Create a post（发 WP 文章，设特色图）
                                                              ▼
                                          16 Send an Email（发 HTML 邮件）
                                                              ▼
                                          17 飞书获取Token（换 tenant_access_token）
                                                              ▼
                                          18 飞书根目录（拿 folder_token）
                                              ├──────────────────────────┐
                                              ▼                          ▼
                                    19 组装搜索存档              27 飞书建文档
                                              ▼                          ▼
                                    20 飞书写搜索表              28 组装飞书文档
                                              │                          ▼
                                              │                  29 飞书写文档内容
                                              │                          ▼
                                              │                  30 设置文档权限
                                              ▼
                                    21 回发获取Token（再换一次 token）
                                              ▼
                                    22 拆分配图（imageUrls → N 个 item）
                                              ▼
                                    23 回发下载配图（逐张下载为二进制）
                                              ▼
                                    24 回发上传图片（逐张换 image_key）
                                              ▼
                                    25 组装群聊回发卡（拼 post 富文本）
                                              ▼
                                    26 发送富文本卡片（im/v1/messages，msg_type=post）
```

> **关键点**：`18 飞书根目录` 之后是**两条并行分支**。左边写多维表格，右边建云文档。群内回发（21）挂在**左边**分支末尾，但它在 25 号节点里会反向引用右边分支产生的文档 ID —— 所以 n8n 会等整个工作流所有分支跑完才结束（实际上 25 执行时 27 已经完成，因为并发执行更快）。

### 1.3 九个阶段一览

| 阶段 | 节点 | 干什么 | 失败影响 |
|---|---|---|---|
| ① 事件接入 | 01–05 | 收飞书事件、校验、先回执 | 全链路不通 |
| ② 联网检索 | 06–07 | searxng 搜网页，拼成素材文本 | 简报无事实依据 |
| ③ AI 生成 | 08 + 33/34/35 | LLM 写 2000 字 JSON 简报 | 无内容可发 |
| ④ 结构化解析 | 09 | 拆出 `brief` 和 `image_keywords` | 后续全部拿不到字段 |
| ⑤ 配图 | 10 / 31 / 32 / 11 / 12 | 每关键词搜 3 张，汇总配额 | 无图（业务仍可继续） |
| ⑥ 多端发布 | 13–16 | WP 特色图 + WP 文章 + 邮件 | 单点失败不阻断（已设 continue） |
| ⑦ 飞书归档 | 17–20 / 27–30 | 多维表格 + 云文档 + 权限 | 归档缺失 |
| ⑧ 群回发 | 21–26 | 上传图 + 发图文卡片 | 群里无反馈 |
| ⑨ 收尾 | — | 执行结束，清理 | — |

### 1.4 外部依赖清单

| 系统 | 地址（示例） | 用途 | 关键约束 |
|---|---|---|---|
| 飞书开放平台 | `open.feishu.cn` | 事件订阅 / 文档 / 多维表格 / 消息 | 需自建应用 + 权限 + 事件订阅 |
| searxng | `http://searxng:8080` | 元搜索（网页 + 图片） | 需在 n8n 容器同网络 |
| Ollama | n8n 内配置的 baseUrl | 本地 LLM 推理 | `qwen3:8b` 显存约 6–8GB |
| WordPress | `http://host.docker.internal:8090` | 文章 + 媒体库 | 需 Application Password |
| MCP 文件服务 | `http://host.docker.internal:3001` | Agent 可调工具 | 挂了不影响主链路 |
| SMTP | 163 邮箱 | 发 HTML 邮件 | 需授权码 |

> **`host.docker.internal` 是什么**：Docker 容器内访问**宿主机**的保留域名（Docker Desktop 内置 DNS）。因为 WordPress/MCP 跑在宿主机或另一个容器，而 n8n 在容器里，用 `localhost` 会指向容器自己，必须用这个域名。Linux 下需要在 `docker run` 时加 `--add-host=host.docker.internal:host-gateway`。

---

## 2. 前置准备与外部依赖

### 2.1 n8n 侧

1. **网络同网**：`searxng` 必须和 n8n 在同一 Docker 网络，否则 `http://searxng:8080` 解析不到。
2. **环境变量**：
   - `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`（若要在表达式里读 `$env`）
   - `WEBHOOK_URL=https://<公网域名>/`（飞书回调必须公网可达，否则事件推不进来）
3. **Webhook 公网暴露**：用 cpolar / frp / nginx 反代到 `localhost:5678`，飞书后台填 `https://<域名>/webhook/feishu-bot-callback`。

### 2.2 飞书自建应用（最容易出错的一环）

**创建**：飞书开放平台 → 开发者后台 → 创建企业自建应用。

**必须开通的权限**（权限管理 → API 权限）：

| 权限码 | 用途 | 对应节点 |
|---|---|---|
| `im:message` | 发消息 | 26 |
| `im:resource`（上传图片） | `im/v1/images` | 24 |
| `docx:document` | 建文档、写内容 | 27 / 29 |
| `drive:drive` | 根目录、权限设置 | 18 / 30 |
| `bitable:app` | 多维表格写入 | 20 |
| `im:message.group_msg`（可选） | 读群消息（**本文验证时未开通**） | 调试用 |

**事件订阅**：
- 订阅方式选「**将事件发送至开发者服务器**」
- 请求地址：`https://<公网域名>/webhook/feishu-bot-callback`
- 添加事件：`im.message.receive_v1`（接收消息）
- 首次保存时飞书会发 `url_verification` 握手，工作流必须能返回 `{"challenge": "..."}`

**发布**：改完权限/事件必须**创建版本并发布**，否则不生效。

> ⚠️ **头号坑：双 App 混乱**
> 文档/多维表格的 owner 是"创建它的那个 App"。如果你早期用 App-A 建过文档，后来换成 App-B，那么：
> - App-B 的 `tenant_access_token` 去改 App-A 的文档权限 → **一律 `permission denied`**
> - 群里用户点开文档 → **无权限**
>
> 判别方法：看 token 前缀。不同 App 换出来的 token 前缀不同（形如 `t-g104au7S****A` 与 `t-g104au7S****B`：前 9 位相同、尾部不同）。
> 解决：**全链路统一同一个 App**。本工作流中 `17 飞书获取Token`、`21 回发获取Token`、`30 设置文档权限` 必须用同一套 `app_id/app_secret`。

### 2.3 searxng

```yaml
# docker-compose 片段
searxng:
  image: searxng/searxng
  environment:
    - SEARXNG_BASE_URL=http://searxng:8080/
  # 必须允许 JSON 输出，否则 /search?format=json 返回 403
```

`settings.yml` 关键项：

```yaml
search:
  formats:
    - html
    - json        # ← 不加这行，HTTP Request 节点会拿到 403
```

### 2.4 Ollama

```bash
ollama pull qwen3:8b
# 容器内的 n8n 要访问宿主机 Ollama，baseUrl 填 http://host.docker.internal:11434
```

### 2.5 WordPress

- 站点需开启 REST API（默认开启）
- 建一个**应用密码**（用户 → 个人资料 → 应用程序密码），用于 HTTP Basic Auth
- 媒体上传需要该用户有 `upload_files` 权限

### 2.6 凭据清单（不要写进代码！）

本仓库用 `.env` 管理真实值，`scripts/sanitize_workflow.py` 负责把运行版工作流里的真实值替换成占位符再入库。

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
WP_PUBLIC_BASE=https://xxxx.r16.cpolar.top
OLD_FEISHU_APP_ID=cli_yyyyyyyyyyyyyyyy
OLD_FEISHU_APP_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
N8N_BASE_URL=http://localhost:5678
```

| 占位符 | 出现位置 | 说明 |
|---|---|---|
| `YOUR_FEISHU_APP_ID` | 17 / 21 节点 jsonBody | 新 App ID |
| `YOUR_FEISHU_APP_SECRET` | 17 / 21 节点 jsonBody | 新 App Secret |
| `YOUR_FEISHU_CHAT_ID` | 25 节点兜底 chat_id | 群 ID |
| `YOUR_WP_PUBLIC_BASE` | 25 节点公网基址 | cpolar 域名 |
| `YOUR_OLD_FEISHU_APP_ID/SECRET` | 仅用于修历史文档 | 旧 App |

---

## 3. 阶段一 · 事件接入（节点 01–05）

这一阶段只有一件事：**把飞书的 HTTP 回调，变成 n8n 内部的一条结构化数据，并且让飞书尽快收到 200。**

### 3.1 节点 01 · 飞书群机器人回调 Webhook

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.webhook` v2 |
| ID | `a1b2c3d4-0001-...-000000000001` |
| 方法 / 路径 | `POST` / `feishu-bot-callback` |
| 响应模式 | `responseNode`（由 Respond to Webhook 节点决定响应体） |

**它做什么**
n8n 启动时注册一条路由 `/webhook/feishu-bot-callback`，等待外部 POST。飞书把事件 JSON POST 过来，这个节点把请求体放进 `$json`，并把请求头放进 `$json.headers`。

**参数逐项**

| 参数 | 取值 | 含义 / 影响 |
|---|---|---|
| HTTP Method | `POST` | 飞书事件推送固定用 POST |
| Path | `feishu-bot-callback` | 最终 URL = `{WEBHOOK_URL}/webhook/feishu-bot-callback` |
| Authentication | `None` | 飞书事件本身不带 n8n 认证；安全性靠"URL 不公开 + 飞书签名校验（本流程未启用）" |
| Response Mode | `responseNode` | **关键**：不立即返回，而是等下游 `Respond to Webhook` 节点决定响应内容。这样才能回 `challenge` |
| Response Data | — | 配合 `responseNode` 时为灰 |
| Options | 空 | 可设 `rawBody`（需原文时）、`responseCode` 等 |

**上下游契约**

- 上游：飞书开放平台（外部 HTTP）
- 下游：`02 飞书事件解析`

**技术机理**
n8n 的 webhook 有两种响应模式：
1. `onReceived`：收到即回 `{"message":"Workflow was started"}`，后续节点异步跑。
2. `responseNode`：挂起 HTTP 响应，直到工作流执行到 `Respond to Webhook` 节点。**飞书 URL 验证要求原样返回 challenge，必须用这个模式**。

**坑与经验**
- 如果路径写错，飞书后台保存事件订阅时会报"请求地址校验失败"。
- `responseNode` 模式下，如果工作流**没有任何** Respond 节点、或执行报错在 Respond 之前，飞书会收到 500/超时，随后**重推**（飞书重试策略：约 15s/5min/… 多次），造成重复执行。所以 Respond 节点必须尽早出现。

---

### 3.2 节点 02 · 飞书事件解析

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.code` v2 |
| 模式 | Run Once for All Items |
| 代码长度 | 1479 字符 |

**它做什么**
把飞书两种事件（URL 验证 / 消息接收）统一解析成内部结构：验证类输出 `{challenge}`，消息类输出 `{chatInput, chat_id, message_id, msg_type, sender}`。

**核心代码与注释**

```js
// n8n Webhook 的请求体通常在 $json.body；此处兼容 顶层对象 / body对象 / body字符串 / data对象
const raw = $json || {};
let body = raw;
if (raw.body !== undefined && raw.body !== null) {
  if (typeof raw.body === 'object') { body = raw.body; }
  else if (typeof raw.body === 'string') {
    try { body = JSON.parse(raw.body); } catch (e) { body = raw; }
  }
}
// 再兜一层：某些代理会把事件包在 data 里
if (!body || (body.type === undefined && body.header === undefined && body.challenge === undefined)) {
  if (raw.data && typeof raw.data === 'object') { body = raw.data; }
}

// 1) URL 验证握手：飞书发 {type:"url_verification", challenge:"xxx"}，必须原样回 challenge
const isVerify = (body.type === 'url_verification') ||
                 (typeof body.challenge === 'string' && body.challenge.length > 0);
if (isVerify) {
  return [{ json: { challenge: body.challenge } }];
}

// 2) 群聊 @ 机器人消息事件
const header = body.header || {};
if (header.event_type === 'im.message.receive_v1') {
  const event = body.event || {};
  const message = event.message || {};
  const chatId = message.chat_id || 'oc_YOUR_FEISHU_CHAT_ID';
  const messageId = message.message_id || '';
  const msgType = message.message_type || 'text';

  let text = '';
  try {
    // message.content 是【字符串化的 JSON】，例如 "{\"text\":\"@_user_1 谈谈人形机器人\"}"
    const c = JSON.parse(message.content || '{}');
    text = c.text || '';
  } catch (e) { text = ''; }

  // 清掉 @ 占位符：飞书把 @ 渲染成 @_user_1，中文场景下 @昵称 也要清
  text = text.replace(/@_user_\d+\s*/g, '').replace(/@[^\s]+\s*/g, '').trim();

  return [{ json: {
    chatInput: text, chat_id: chatId, message_id: messageId,
    msg_type: msgType, sender: (event.sender || {}).sender_id || null
  } }];
}

// 3) 其它事件（已读回执、群成员变更等）直接忽略
return [{ json: { skipped: true } }];
```

**为什么 `message.content` 要 `JSON.parse` 两次？**
飞书事件体里 `content` 字段是**字符串**，其内容是另一段 JSON。这是飞书 API 的通用约定（消息内容随类型变化，用字符串保证 schema 稳定）。

**坑与经验**
- 正则 `@[^\s]+\s*` 会把用户正文里以 `@` 开头的东西也吃掉。如果主题里带 `@`，建议改成只清 `@_user_\d+`。
- 中文被 `decodeURIComponent` 过（n8n 自动处理），不要手动再解码。
- `chat_id` 兜底值写死是有意为之：调试期可直接触发，不必真发群消息。

---

### 3.3 节点 03 · 验证握手?

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.if` v2 |
| 条件 | `{{ $json.challenge ? 'Y' : 'N' }}` **等于** `Y` |

**它做什么**
一个二选一分流器：有 `challenge` 字段 → 走 true 分支（验证）；没有 → 走 false 分支（正常业务）。

**参数逐项**

| 项 | 说明 |
|---|---|
| Combinator | `and`（只有一个条件，无所谓） |
| Type Validation | `strict`：类型不匹配直接判 false，不做隐式转换 |
| Case Sensitive | 开启（比较 `Y`） |

**为什么用 `$json.challenge ? 'Y' : 'N'` 而不是直接判断"存在"？**
n8n IF 节点的"exists"操作符在某些版本下对 `undefined` / 空字符串行为不一致。转成显式字符串比较，跨版本最稳。

**坑与经验**
- IF 节点的 **true 输出是 `main[0]`，false 是 `main[1]`**。连线时如果接反，会把验证请求送到业务链路（拿不到 `chatInput`，全链路空跑）。

---

### 3.4 节点 04 · Respond 验证

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.respondToWebhook` v1 |
| Respond With | `firstIncomingItem` |

**它做什么**
把上游 item 的 JSON **原样作为 HTTP 响应体**返回。上游是 `{challenge: "xxx"}`，所以返回 `{"challenge":"xxx"}`，飞书校验通过。

**参数逐项**

| 参数 | 取值 | 说明 |
|---|---|---|
| Respond With | `firstIncomingItem` | 取第一条 item 的 json 当响应体 |
| Options → Response Code | 默认 200 | 飞书要求 200 |
| Options → Response Headers | 默认 `Content-Type: application/json` | — |

**技术机理**
`responseNode` 模式下，webhook 的 HTTP 连接被挂起。Respond 节点一旦执行，n8n 立刻写出响应并关闭连接。

**坑与经验**
- 只能在 `responseNode` 模式下用。用 `onReceived` 模式时这个节点会报错。
- 一次执行**只能有一个** Respond 节点被触发。分支设计上要保证互斥。

---

### 3.5 节点 05 · Respond 回执

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.respondToWebhook` v1 |
| Respond With | `firstIncomingItem` |

**它做什么**
把 `{chatInput, chat_id, ...}` 原样返回给飞书，飞书收到 200 后认为"事件已消费"，停止重推。

**为什么必须先回执，而不能等全流程跑完再回？**

| 约束 | 数值 | 后果 |
|---|---|---|
| 飞书事件回调超时 | **3 秒** | 超时即判失败 |
| 本流程实际耗时 | **60–120 秒**（LLM 生成 + 6 张图下载上传 + 多次 API） | 必超时 |
| 飞书重试 | 失败后多次重推 | 同一主题被重复处理 N 次：重复发邮件、重复建文档、重复发群消息 |

所以**回执必须在下游重活之前**。这也解释了为什么 05 号节点放在 `06 HTTP Request` 之前，而不是最后。

**技术机理：n8n 的"响应后继续执行"**

在 `responseNode` 模式下，n8n 内部把响应和后续执行解耦：
1. Respond 节点执行 → 立即 flush HTTP 响应；
2. 工作流继续往下跑（异步）；
3. 若后续节点报错，飞书那边**已经收到 200，不会重推**——所以错误只能在 n8n 执行记录里看到，需要自己监控。

**坑与经验**
- 这个设计换来的是"不重复"，代价是"错误静默"。生产环境建议加一个"失败告警"分支（见附录 D）。
- 调试时如果想让飞书等到结果（比如想在群消息里拿最终链接），改回 `onReceived` 会让飞书超时重推，**不建议**。

---

## 4. 阶段二 · 联网检索（节点 06–07）

这一阶段把"用户说的那句话"变成"带出处的事实素材"。

### 4.1 节点 06 · HTTP Request（searxng 网页检索）

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.httpRequest` v4.5 |
| URL | `http://searxng:8080/search` |
| 方法 | `GET`（默认） |
| 超时 / 重试 | 20000ms / 2 次，间隔 1.5s |

**它做什么**
调用自建 searxng 元搜索引擎，用用户主题做一次综合网页检索，拿回 JSON 结果列表。

**参数逐项**

| 参数 | 取值 | 含义 / 影响 |
|---|---|---|
| URL | `http://searxng:8080/search` | **容器名**寻址。n8n 与 searxng 必须在同一 Docker 网络；宿主机访问要用 `localhost:8080` |
| Send Query | on | 开启 query string 拼接 |
| `q` | `={{ $json.chatInput }}` | 用户主题原文。中文需 URL 编码，n8n 自动处理 |
| `format` | `json` | **关键**：searxng 默认只输出 HTML；必须在 `settings.yml` 的 `search.formats` 里加 `json`，否则 403 |
| `language` | `zh-CN` | 结果排序倾向中文源 |
| `categories` | `general` | 综合网页（另有 `images`/`news`/`science` 等） |
| Timeout | 20000 | searxng 聚合多个上游引擎，慢的时候 5–15s 很常见；默认超时（通常 10s）会频繁失败 |
| Retry On Fail | 2 次 | 上游引擎偶发限流，重试可显著提升有结果率 |

**上下游契约**

- 上游：`05 Respond 回执` → 输出 `{chatInput, chat_id, message_id, msg_type, sender}`
- 下游：`07 Code in JavaScript`
- 输出结构（searxng）：
```json
{
  "query": "人形机器人产业",
  "results": [
    { "title": "...", "url": "https://...", "content": "摘要文本...", "engine": "bing" }
  ],
  "answers": [], "infoboxes": [], "suggestions": []
}
```

**技术机理**
searxng 本身不建索引，而是**并发转发**给 Google/Bing/百度等上游，再合并去重。所以它的延迟取决于最慢的那个上游；且**上游限流时会整体返回 0 条**（这正是本工作流曾经遇到的场景）。

**坑与经验**
- **`results: []` 不等于出错**。HTTP 200 但结果为空是常态（生僻主题、被限流）。必须在下游显式处理空结果，否则会连锁引发 AI 幻觉（见 4.2）。
- 容器名 `searxng` 是 docker-compose 里的 service 名。若你的服务叫别的名字，这里必须改。
- 如果 n8n 报 `ENOTFOUND searxng`，说明两者不在同一网络：`docker network inspect` 检查。

---

### 4.2 节点 07 · Code in JavaScript（拼装检索素材）

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.code` v2 |
| 模式 | Run Once for All Items |
| 代码长度 | 约 1100 字符 |

**它做什么**
把 searxng 的 JSON 结果压成一段**人类可读的素材文本**，供 LLM 阅读；同时检测"是否为空"，把信号透传给下游。

**核心代码与注释**

```js
const data = $input.first().json;
const list = Array.isArray(data.results) ? data.results : [];

// 取前 6 条，拼成「编号 + 标题 + 链接 + 摘要」的结构
const text = list.slice(0, 6).map((r, i) =>
  `【${i + 1}】${r.title || '无标题'}\n链接：${r.url || ''}\n摘要：${r.content || '（无摘要）'}`
).join('\n\n');

const trig = $('飞书事件解析').first().json;

// 【重要】检索为空的兜底
// 实测：searxng 上游引擎偶发限流会返回 0 条。
// 若不提示，LLM 会凭空编造与主题无关的内容（实例：问「低空经济」，
// 模型编出了「新能源汽车销量」整篇简报，配图也跟着跑偏）。
// 这里显式给出「素材不足」信号，让模型只基于主题本身作答，并把标记透传给下游。
const noMaterial = text.length === 0;
const searchText = noMaterial
  ? `（本次联网检索未返回任何结果，主题为「${trig.chatInput}」。请仅依据该主题的通识知识撰写，禁止编造具体数据、机构名称与时间点；无法确认的内容一律使用定性描述。）`
  : text;

return [{
  json: {
    query: trig.chatInput,
    search_text: searchText,
    noMaterial,                       // ← 供 09 号节点决定配图关键词策略
    hitCount: list.length,
    sessionId: trig.chat_id || 'feishu-group'   // ← 给 Simple Memory 用
  }
}];
```

**为什么只取 6 条？**

| 条数 | 上下文占用 | 效果 |
|---|---|---|
| 3 条 | ~1.5k token | 信息不足，容易写空 |
| **6 条** | **~3k token** | 够写 6 小节，且不挤占输出空间 |
| 10 条 | ~5k token | 收益递减，还容易被无关内容带偏 |

`qwen3:8b` 的 `numCtx` 是 16384，6 条素材 + 2000 字输出绰绰有余。

**坑与经验**
- `join('\n\n')` 用**双换行**分段，下游 `组装搜索存档` 正是用 `split(/\n\s*\n/)` 还原条目数——两处必须保持一致。
- `noMaterial` 这个标记虽小，却是**防幻觉的关键开关**：实测加了这个提示之后，同样的空结果场景不再出现跑题内容。

---

## 5. 阶段三 · AI 简报生成（节点 08 / 33 / 34 / 35）

这是全流程唯一的"智能"环节，也是耗时最长的（30–60 秒）。

### 5.1 节点 08 · AI Agent

| 项 | 值 |
|---|---|
| 类型 | `@n8n/n8n-nodes-langchain.agent` v3.1 |
| Prompt Type | `define`（自定义提示词） |
| 挂接子节点 | `ai_languageModel` → 33 Ollama；`ai_tool` → 34 MCP；`ai_memory` → 35 Memory |

**它做什么**
把素材喂给 LLM，要求它输出**严格 JSON**：一段 2000 字、6 小节的简报正文 + 2 个配图关键词。

**提示词全文（含逐条解读）**

```
你是资深产业分析师。请基于下面的检索素材，写一份中文深度简报。

硬性要求：
1. 必须分为 6 个小节，每个小节以【】包裹的中文小标题开头，每节正文不少于 300 字，整体正文约 2000 字。
2. 每节要有具体数据、机构/公司名称或时间点，禁止空话套话。
3. 不写"总结""结语"类收尾段落，写满第 6 节即结束。
4. 最后提炼 2 个适合配图的中文关键词。

输出格式必须是严格 JSON，不要 markdown 代码块，不要任何解释文字：
{"brief": "简报正文（含【】小标题，用\n换行）", "image_keywords": ["关键词1", "关键词2"]}

检索素材：
{{ $json.search_text }}
```

| 约束 | 为什么这么写 |
|---|---|
| **6 小节 × 300 字** | 明确的结构下限。只写"约 2000 字"，模型容易写成 800 字；给了 6×300 的乘法锚点，实测稳定在 1800–2200 字 |
| **以【】包裹小标题** | 给正文一个**可解析的结构标记**。飞书文档/邮件里能当小标题渲染，也便于人工快速扫读 |
| **禁止空话套话，要具体数据** | 抑制小模型最爱的"随着科技发展…具有重要意义" |
| **不写总结/结语** | 群消息卡片空间有限；且"总结"最容易重复前文，浪费 token |
| **提炼 2 个关键词** | **直接决定配图数量**：2 词 × 3 张 = 6 张。改这个数字必须同步改 09 号节点的 `MAX_KEYWORDS` |
| **严格 JSON、不要 markdown 代码块** | 小模型习惯性套 ` ```json `，会导致 `JSON.parse` 失败。明令禁止后命中率大幅提升 |
| **`{{ $json.search_text }}`** | n8n 表达式，注入 07 号节点拼好的素材 |

**上下游契约**

- 上游：`07 Code in JavaScript` → `{query, search_text, noMaterial, hitCount, sessionId}`
- 下游：`09 Code in JavaScript1`
- 输出：LangChain Agent 的返回对象，正文在 `json.text`（也可能是 `output` / `content` / `message.content`，视版本而定，所以 09 号节点做了四路兼容）

**技术机理**
`AI Agent` 与普通 LLM 节点的区别：它会**自主决定是否调用工具**（ReAct 循环）。本流程挂了 `MCP Client2`，理论上 Agent 可以主动去调 MCP 工具（如读文件、查数据库）。但当前提示词要求"只输出 JSON"，实测 Agent 基本不调工具，纯靠 Ollama 生成。

> 换句话说：这里的 Agent 更像"带工具兜底的 LLM 节点"。若想让它真正用工具，需要在提示词里显式要求（如"先用 file_search 工具查内部资料"）。

**坑与经验**
- Prompt Type 选 `define` 才能自定义；选 `auto` 会走 LangChain 默认模板，输出格式不受控。
- Agent 的 `maxIterations` 默认较低，若真让它多轮调工具，需要调高。
- **不要在这个节点开 `executeOnce`**：它是单 item 输入，开了也没意义。

---

### 5.2 节点 33 · Ollama Chat Model（模型接入）

| 项 | 值 |
|---|---|
| 类型 | `@n8n/n8n-nodes-langchain.lmChatOllama` v1 |
| Model | `qwen3:8b` |
| temperature | `0.4` |
| numCtx | `16384` |
| numPredict | `4096` |

**参数逐项**

| 参数 | 取值 | 含义 / 为什么这么设 |
|---|---|---|
| Model | `qwen3:8b` | 中文产业分析能力尚可，8B 在消费级显卡（8–12GB）能跑 |
| **temperature** | `0.4` | 越低越确定。0.7 会"发挥"，写出一堆形容词；0.2 又太死板、句式重复。0.4 是实测平衡点 |
| **numCtx** | `16384` | 上下文窗口。素材 ~3k token + 提示词 ~0.5k + 输出 ~3k，16k 足够且不浪费显存 |
| **numPredict** | `4096` | **最大输出 token**。2000 汉字 ≈ 3000+ token；设 4096 留足余量。设太小会**中途截断**（JSON 不完整 → 解析失败 → 全链路崩） |

**技术机理**
`numPredict` 是 Ollama 的硬截断：模型生成到该 token 数就停，不管句子是否写完。**这是 JSON 解析失败的头号原因**。判断方法：看 09 号节点输出，若 `brief` 以半句话结尾且没有 `"image_keywords"` 字段，就是被截断了。

**坑与经验**
- 从 `6144` 降到 `4096` 是在把目标字数从 3000 降到 2000 之后同步调整的——**字数和 numPredict 必须联动**，否则要么浪费显存，要么截断。
- `qwen3` 系列默认会输出"思考过程"（`...`）。若发现模型把思考内容也吐进 JSON，需要在模型名前加 `:` 关闭思考，或在提示词里明确"不要输出思考过程"。

---

### 5.3 节点 34 · MCP Client2（工具通道）

| 项 | 值 |
|---|---|
| 类型 | `@n8n/n8n-nodes-langchain.mcpClientTool` v1.4 |
| Endpoint URL | `http://host.docker.internal:3001` |
| Timeout | 60000ms |

**它做什么**
把外部 MCP Server 暴露的工具集，注册给 AI Agent 使用。

**技术机理**
MCP（Model Context Protocol）是 Anthropic 提出的"模型 ↔ 工具"标准协议。n8n 作为 MCP Client 连上 Server 后，会 `list_tools` 拿到工具清单，再在 Agent 循环里按需 `call_tool`。

> **用 `host.docker.internal` 而不是 `mcp-files:3001`**：实测在 n8n 容器内访问容器名 `mcp-files` 会 TIMEOUT，走宿主机端口映射反而稳定返回 200。这是本环境的一个已知现象。

**坑与经验**
- 这个服务**挂了不影响主链路**：Agent 只是少了一个工具，生成能力不受影响。所以不需要为它加 `onError`。
- 如果 MCP server 启动慢，Agent 首轮会等到 timeout。60s 是宽松值。

---

### 5.4 节点 35 · Simple Memory（会话记忆）

| 项 | 值 |
|---|---|
| 类型 | `@n8n/n8n-nodes-langchain.memoryBufferWindow` v1.4 |
| Session ID Type | `customKey` |
| Session Key | `={{ $('飞书事件解析').first().json.chat_id }}` |
| Context Window Length | `10` |

**它做什么**
按**群 ID** 维护最近 10 轮对话历史，让模型能"记得"同一群里刚才聊过什么。

**参数逐项**

| 参数 | 说明 |
|---|---|
| Session ID Type | `customKey` = 用自定义表达式做会话键（另有 `fromInput` 等） |
| Session Key | 用 `chat_id` → **每个群一个独立记忆**，不会串味 |
| Context Window Length | 保留最近 10 轮。越大越占 token，10 轮约增加 2–3k token |

**为什么本流程其实"用不上"记忆？**

因为提示词写死了"基于下面检索素材写简报"，模型没有"接着上文聊"的余地。记忆在这里更像**预留能力**：若改成对话式问答（"刚才那个数据再说细点"），它立刻就有用。

**坑与经验**
- Session Key 用了 `.first()`：多 item 场景下取第一条的 chat_id，足够（同一事件只对应一个群）。
- 记忆是**进程内**的，n8n 重启即清空。要持久化得换 Redis/Postgres 版 Memory。

---

## 6. 阶段四 · 结构化解析（节点 09）

### 6.1 节点 09 · Code in JavaScript1

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.code` v2 |
| ID | `3d475114-f351-4281-a71d-b0ac5c4fda0f` |

**它做什么**
三件事：①从 LLM 的"可能不干净"的输出里抠出 JSON；②确定配图关键词；③把 1 个 item 拆成 `1 + N` 个 item（简报 + 每个关键词一条搜索任务）。

**核心代码与注释**

```js
const items = $input.all();
if (items.length === 0) return items;

const output = items[0].json;

// 1. 兼容四种 Agent 返回字段名（不同 n8n/LC 版本不一样）
let rawText = output.text || output.output || output.content || output.message?.content || '';

// 2. 从可能夹带前言/代码块的文本里抠出最外层 JSON
let jsonMatch = rawText.match(/[\s\S]*\{[\s\S]*\}[\s\S]*/);
if (!jsonMatch) jsonMatch = [rawText];

let parsed = {};
try {
    parsed = JSON.parse(jsonMatch[0]);
} catch (error) {
    // 3. 解析失败时用正则兜底，尽量抢救出 brief 与关键词
    const briefMatch = rawText.match(/"brief"\s*:\s*"([^"]*)"/);
    const keysMatch = rawText.match(/"image_keywords"\s*:\s*\[(.*?)\]/s);
    parsed = {
        brief: briefMatch ? briefMatch[1] : rawText,
        image_keywords: keysMatch ? keysMatch[1].split(',').map(s => s.trim().replace(/"/g, '')) : []
    };
}

const brief = parsed.brief || rawText;
const query = $('飞书事件解析')?.first()?.json?.chatInput || '未知主题';

// 上游检索是否为空；节点重跑时可能取不到，做 try 兜底
let noMaterial = false;
try { noMaterial = $('Code in JavaScript')?.first()?.json?.noMaterial === true; } catch (e) { noMaterial = false; }

// 4. 关键词清洗
//    【关键设计】把 query（用户原始主题）作为第一个关键词。
//    原因：实测 LLM 在检索素材为空时会「幻觉」出与主题无关的词
//    （例：问低空经济，却给出新能源汽车），用主题本身兜底可保证配图不跑偏。
const MAX_KEYWORDS = 2;            // 2 个关键词 × 每词 3 张 = 6 张图（与产品预期一致）
const seenKw = new Set();
const keywords = [];

function addKw(k) {
    const kw = String(k == null ? '' : k).trim();
    if (!kw) return false;
    const sig = kw.toLowerCase();
    if (seenKw.has(sig)) return false;   // 忽略大小写去重
    seenKw.add(sig);
    keywords.push(kw);
    return true;
}

// 4.1 主题本身优先（过长时截断：搜索词过长会显著降低命中率）
addKw(String(query).replace(/[，。！？、,.!?]/g, ' ').trim().slice(0, 20));

// 4.2 再补 LLM 给出的细分关键词
const rawKeywords = Array.isArray(parsed.image_keywords) ? parsed.image_keywords : [];
for (const k of rawKeywords) {
    if (keywords.length >= MAX_KEYWORDS) break;
    addKw(k);
}

const results = [];

// 5. 第 1 条：简报数据（走 Switch 的 brief 分支，最终进 Merge）
results.push({
    json: { type: 'brief', query, brief, keywords, noMaterial }
});

// 6. 其余 N 条：每个关键词一条图片搜索任务
//    下游 HTTP Request1 会逐 item 执行 → 每个关键词各搜一次
keywords.forEach((kw, i) => {
    results.push({
        json: {
            type: 'image_search',
            query, keyword: kw,
            kwIndex: i + 1,
            kwTotal: keywords.length
        }
    });
});

return results;
```

**上下游契约**

| 方向 | 节点 | 数据 |
|---|---|---|
| 输入 | 08 AI Agent | `[{json:{text: "..."}}]` |
| 输出 | 10 Switch | `[{type:'brief',...}, {type:'image_search',keyword:...}, ...]` |

**为什么第 1 个关键词必须是用户主题？**

这是本项目**踩坑后加的设计**。曾出现：用户问"低空经济eVTOL商业化路径"→ searxng 返回 0 条 → 模型幻觉出"新能源汽车"→ 关键词也变成"新能源汽车销量增长"→ 配图全是汽车。整条链路"看起来正常"，但内容和主题完全无关，**比报错更危险**。

把 `query` 钉在第一位，等于给配图上了"主题锚"，即使 LLM 跑偏，至少有一组图是对的。

**坑与经验**
- `[\s\S]*\{[\s\S]*\}` 是贪婪匹配：如果模型输出了两段 JSON，会取从第一个 `{` 到最后一个 `}` 的整段，可能解析失败。更稳的写法是 `/\{[\s\S]*\}/` + 逐个尝试，但当前提示词足够严格，实测够用。
- `slice(0, 20)` 截断中文主题：searxng 对超长查询的召回率会明显下降。
- `typeValidation: strict` 的 Switch 依赖 `type` 字段**严格等于** `brief` / `image_search`，改字符串时两处必须同步。

---

## 7. 阶段五 · 配图流水线（节点 10 / 31 / 32 / 11 / 12）

这是本工作流最"绕"的一段，因为它要**在同一个下游节点里，同时处理两条语义完全不同的数据流**（简报文本 / 图片列表）。

```
09 Code1 ──► 10 Switch ──┬─ brief ─────────────────────────────► 11 Merge ──► 12 Code3
                         └─ image_search ► 31 HTTP Request1 ► 32 Code2 ──► 11 Merge
```

### 7.1 节点 10 · Switch（分流器）

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.switch` v3.4 |
| 规则数 | 2 |

**参数逐项**

| 输出分支 | 条件 | 去向 |
|---|---|---|
| output 0 | `{{ $json.type }}` equals `brief` | 11 Merge（input 0） |
| output 1 | `{{ $json.type }}` equals `image_search` | 31 HTTP Request1 |
| fallback | 未匹配 | 无（丢弃） |

**技术机理**
Switch 按**输入 item 逐条**判断，把每条 item 路由到对应输出口。所以 Code1 输出的 3 条 item（1 条 brief + 2 条 image_search）会被拆成 1 条和 2 条，分别走两条路。

**坑与经验**
- **连线索引**：`main[0]` 是第一条规则，`main[1]` 是第二条。接反会导致 brief 去搜图、关键词去 Merge，全乱。
- Switch v3 与 v2 的条件结构不兼容（v3 用 `conditions.conditions[]`）。跨版本复制节点容易出问题。

---

### 7.2 节点 31 · HTTP Request1（图片检索，逐关键词执行）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `http://searxng:8080/search` |
| 超时 / 重试 | 20000ms / 2 次 |

**参数逐项**

| 参数 | 取值 | 说明 |
|---|---|---|
| `q` | `={{ $('Code in JavaScript1').item.json.keyword }}` | **`item` 而非 `first`**：取"当前这条 item 对应的上游 item"，所以 2 条任务会搜 2 个不同关键词 |
| `format` | `json` | 同上，searxng 必须开 json |
| `categories` | `images` | **图片专用分类**，返回字段与 general 完全不同 |
| `language` | `zh-CN` | 中文图源优先 |
| `num_results` | `8` | 每关键词最多 8 条候选。**从 5 提到 8** 是为了给下游"挑 6 张候选 + 剔除坏图"留余量 |

**返回结构（images 分类）**

```json
{
  "results": [
    {
      "title": "图片标题",
      "url": "图片所在网页",
      "img_src": "https://cdn.example.com/real-image.jpg",   // ← 真实图片直链
      "thumbnail_src": "https://kkimgs.yisou.com/ims?kt=...",// ← 搜索引擎缩略图代理
      "img_format": "jpeg",
      "source": "bing images"
    }
  ]
}
```

**技术机理：`$('节点名').item` 是什么**

n8n 为每条 item 维护一条"血缘链"（paired item）。`$('X').item` 表示"**跟我同源的那条 X 的输出**"。这让"一个节点跑 N 次"能拿到 N 个不同的参数值。

**坑与经验**
- 如果 Code1 输出结构变了（比如少了 `keyword`），这里会拿到 `undefined`，searxng 会返回通用热门图，**看起来有图但全不相干**。
- `num_results` 是"最多"，searxng 经常给不满。

---

### 7.3 节点 32 · Code in JavaScript2（挑图 + 过滤）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| 模式 | Run Once for All Items |

**它做什么**
对每个关键词的 searxng 结果，挑出最多 6 张"像真图片、且大概率能下载"的直链，并打上关键词标签。

**核心代码与注释**

```js
const PER_KEYWORD = 6;   // 每关键词最多挑几张候选（下游 Code3 再按配额截到 3 张）

// 扩展名白名单 / 黑名单
const EXT_OK  = /\.(jpe?g|png|webp|gif|bmp)(\?|#|$)/i;   // 飞书 im/v1/images 支持
const EXT_BAD = /\.(svg|ico|tiff?)(\?|#|$)/i;            // 飞书不支持
const BAD_HOST = /(toutiaoimg\.com|pstatp\.com|byteimg\.com)/i;  // 强防盗链，实测必 403

// 与 HTTP Request1 的输入一一对应（同为 Code1 的 image_search 分支，顺序一致）
let kwItems = [];
try {
    kwItems = $('Code in JavaScript1').all().filter(i => i.json && i.json.type === 'image_search');
} catch (e) { kwItems = []; }

function pickUrl(r) {
    const cand = [r.img_src, r.thumbnail_src, r.thumbnail]
        .filter(u => typeof u === 'string' && /^https?:\/\//i.test(u));

    // 优先级 1：扩展名明确、且不在黑名单
    for (const u of cand) {
        if (!EXT_OK.test(u)) continue;
        if (EXT_BAD.test(u)) continue;
        if (BAD_HOST.test(u)) continue;
        return u;
    }
    // 优先级 2：没有明确扩展名时，取第一个非防盗链的
    // （如 kkimgs.yisou.com/ims?kt=url... 无扩展名，但通常可下载）
    for (const u of cand) {
        if (EXT_BAD.test(u)) continue;
        if (BAD_HOST.test(u)) continue;
        return u;
    }
    return '';
}

const out = [];
let seq = 0;

$input.all().forEach((it, idx) => {
    const data = (it && it.json) || {};
    const list = Array.isArray(data.results) ? data.results : [];
    // 用 idx 对齐关键词（HTTP Request1 是逐 item 执行，顺序与输入一致）
    const kw = (kwItems[idx] && kwItems[idx].json.keyword)
            || (kwItems[0] && kwItems[0].json.keyword) || '';

    const images = [];
    const seenUrl = new Set();
    for (const r of list) {
        if (images.length >= PER_KEYWORD) break;
        const url = pickUrl(r);
        if (!url) continue;
        const base = url.split('?')[0];      // 忽略 query string 去重
        if (seenUrl.has(base)) continue;
        seenUrl.add(base);
        images.push({
            index: seq + images.length + 1,
            keyword: kw,
            title: r.title || kw || '相关配图',
            pageUrl: r.url || '',
            img_src: url
        });
    }
    seq += images.length;

    out.push({ json: { type: 'image_result', keyword: kw, kwIndex: idx + 1, count: images.length, images } });
});

return out.length ? out : [{ json: { type: 'image_result', keyword: '', kwIndex: 0, count: 0, images: [] } }];
```

**为什么要挑 6 张而只展示 3 张？**

因为图片直链**会失效**。实测 6 张里平均有 0–1 张下载失败（403 / SSL / 超时）。多挑几张留冗余，Code3 再按"每词 3 张"截断——这样即使坏掉 1 张，最终仍能凑够。

**坑与经验**
- `$('Code in JavaScript1').all()` 在 Code 节点里可靠；用 `$input` 拿不到关键词（searxng 返回体里没有 keyword）。
- 顺序对齐依赖"HTTP Request1 逐 item 执行且不并行乱序"。n8n 保证输入顺序，实测稳定。
- **`thumbnail_src` 通常是 `kkimgs.yisou.com` 这类代理**，比原站直链更稳（代理会缓存），但分辨率低。所以优先 `img_src`。

---

### 7.4 节点 11 · Merge（汇流）

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.merge` v3.2 |
| 模式 | 默认（append，追加合并） |

**它做什么**
把 `brief`（1 条）和 `image_result`（N 条）**追加成一个列表**：`[brief, image_result1, image_result2, ...]`，一起送进 Code3。

**技术机理**
Merge v3 有四种模式：

| 模式 | 行为 | 本流程是否适用 |
|---|---|---|
| **append** | 所有输入首尾拼接 | ✅ 用这个 |
| combine (merge by key) | 按字段配对 | ❌ 两边字段完全不同 |
| combine (multiplex) | 笛卡尔积 | ❌ 会爆炸 |
| choose branch | 只取一个分支 | ❌ |

**坑与经验**
- Merge 有**两个输入口**（input 0 / input 1），连线时必须分别接，接错口在某些模式下会等不到数据而挂起。
- 若某一侧**完全没有 item**（比如 0 个关键词），Merge 在 append 模式下仍会输出另一侧（这点很重要：无关键词时简报不会丢）。

---

### 7.5 节点 12 · Code in JavaScript3（汇总 + 配额 + 邮件 HTML）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| 模式 | Run Once for All Items |
| **executeOnce** | **true** |
| 代码长度 | 约 5.5k 字符 |

**它做什么**
四件事：①把所有来源的图片候选汇总去重；②按"每词 3 张、总量 6 张、不足则补"分配；③生成 HTML 邮件；④输出 `imageUrls` 数组给回发链路逐张上传。

**关键代码与注释**

```js
// 【为什么要多路兜底拉数据】
// 实证执行 #5774：节点重跑 / paired-item 过滤时，$input 会丢 item。
// 所以同时按节点名补拉 Merge / Code2 / Code1 的输出，再按 JSON 去重。
function safeAll(name) {
  try {
    const ref = $(name);
    return (ref && typeof ref.all === 'function') ? ref.all() : [];
  } catch (e) { return []; }
}
const pool = [];
const seenJson = new Set();
const sources = []
  .concat($input.all() || [])
  .concat(safeAll('Merge'))
  .concat(safeAll('Code in JavaScript2'))
  .concat(safeAll('Code in JavaScript1'));
for (const it of sources) {
  if (!it || !it.json) continue;
  const k = JSON.stringify(it.json);
  if (seenJson.has(k)) continue;
  seenJson.add(k);
  pool.push(it.json);
}

// ===== 配额（两轮分配）=====
const MAX_PER_KEYWORD = 3;    // 第一轮：每词最多 3 张
const TARGET_IMAGES   = 6;    // 目标总量
const MAX_IMAGES      = 9;    // 硬上限
const BAD_URL  = /\.(svg|ico|tiff?)(\?|#|$)/i;
const BAD_HOST = /(toutiaoimg\.com|pstatp\.com|byteimg\.com)/i;

let brief = '', query = '';
const kwList = [];
const imgCandidates = [];

function pushImg(url, title, keyword) {
  if (typeof url !== "string") return;
  if (!/^https?:\/\//i.test(url)) return;
  if (url.includes('.html') || url.includes('search?q') || url.includes('lemmaId=')) return;
  if (BAD_URL.test(url)) return;
  if (BAD_HOST.test(url)) return;
  imgCandidates.push({ url, title: title || keyword || "相关配图", keyword: keyword || "" });
}

// 遍历所有来源，收集候选图
for (const j of pool) {
  if (j.query) query = j.query;
  if (j.brief) brief = j.brief;
  if (j.keyword && !kwList.includes(j.keyword)) kwList.push(j.keyword);
  pushImg(j.img_src, j.title, j.keyword);
  pushImg(j.thumbnail, j.title, j.keyword);
  if (Array.isArray(j.images)) {
    for (const im of j.images) {
      pushImg(im.img_src, im.title, im.keyword || j.keyword);
      pushImg(im.thumbnail, im.title, im.keyword || j.keyword);
    }
  }
}

// 1) 全局按 base url 去重
const seen = new Set();
const dedup = [];
for (const c of imgCandidates) {
  const base = c.url.split("?")[0];
  if (seen.has(base)) continue;
  seen.add(base);
  dedup.push(c);
}

// 2) 第一轮：每关键词最多 3 张 —— 保证「每个主题 3 张」的语义
const perKw = new Map();
const picked = [];
for (const c of dedup) {
  const kwKey = c.keyword || "__none__";
  const n = perKw.get(kwKey) || 0;
  if (n >= MAX_PER_KEYWORD) continue;
  perKw.set(kwKey, n + 1);
  picked.push(c);
}

// 3) 第二轮：总量不足 6 张时，用剩余候选补足
//    （实测 searxng 对部分细分词返回 0 图，不补会出现「只有 3 张」）
if (picked.length < TARGET_IMAGES) {
  const taken = new Set(picked.map((c) => c.url.split("?")[0]));
  for (const c of dedup) {
    if (picked.length >= TARGET_IMAGES) break;
    const base = c.url.split("?")[0];
    if (taken.has(base)) continue;
    taken.add(base);
    picked.push(c);
  }
}

const uniq = picked.slice(0, MAX_IMAGES);
```

**邮件 HTML 生成（要点）**

```js
// 邮件内图片一律用【原图直链】
// 曾试过 images.weserv.nl 图片代理做缩放，但本网络 ECONNRESET 不可达（收件人在国内同理）
let imagesHtml = "";
for (const c of uniq) {
  imagesHtml += `
    <div style="margin:0 auto 36px;text-align:center;page-break-inside:avoid;">
      <img src="${esc(c.url)}" alt="${esc(c.title)}"
           style="max-width:100%;height:auto;border-radius:12px;
                  box-shadow:0 6px 16px rgba(0,0,0,0.08);display:block;margin:0 auto;">
      <p style="font-size:13px;color:#888;margin-top:12px;font-style:italic;">📍 ${esc(c.title)}</p>
    </div>`;
}
```

设计要点：
- `max-width:100%`：邮箱客户端宽度不一，百分比最安全
- `page-break-inside:avoid`：打印/PDF 时单张图不被切页
- 每张图配一行说明（`📍 标题`），比纯图堆更有信息量
- **正文段落**用 `text-indent:2em` 做中文首行缩进

**输出字段**

| 字段 | 用途 |
|---|---|
| `htmlEmail` | → 16 Send an Email |
| `featuredImageUrl` | → 13 HTTP Request2（下载首图做 WordPress 特色图） |
| `imageUrls[]` | → 22 拆分配图（逐张上传飞书） |
| `imageCount` | → 25 组装群聊回发卡（显示"配图 N 张"） |
| `keywords[]` | → 25（标题/记录用） |
| `brief` / `query` | → 15/16/19/25/28 |

**为什么 `executeOnce: true`？**

Code3 的输入是 Merge 输出的多条 item（brief + N 个 image_result）。如果不设 `executeOnce`，n8n 会**对每条 item 各跑一次**，生成 N+1 份邮件、N+1 个文档。设了之后只跑一次，用 `$input.all()` 自己遍历。

**坑与经验**
- 这是最容易出错的一个节点：**任何一处 `undefine` 都会导致后面的 13/15/16/25 拿不到数据**。
- `esc()` 必须转义 `&` **在 `<` 之前**，否则 `&lt;` 会被二次转义成 `&amp;lt;`。
- `keywords` 列表要做 `includes` 去重——同一个词可能来自多个来源。

---

## 8. 阶段六 · 多端发布（节点 13–16）

从 Code3 往后，数据开始"分叉落到不同系统"。这一段是**串行**的，因为 WordPress 特色图必须先上传媒体库、拿到 `media_id`，才能设给文章。

### 8.1 节点 13 · HTTP Request2（下载首图）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `={{ $('Code in JavaScript3').first().json.featuredImageUrl }}` |
| Response Format | `file`（二进制） |
| onError | `continueRegularOutput` |

**它做什么**
把首图下载到 n8n 的 binary 数据里，供下一步上传 WordPress。

**参数逐项**

| 参数 | 说明 |
|---|---|
| URL | 引用 Code3 的首图直链。**用 `.first()`**：Code3 设了 `executeOnce`，只输出 1 条 |
| Response Format = `file` | **关键**：默认是 `json`（会把二进制当文本解析，导致乱码/失败）。选 `file` 后数据进 `$binary.data` |
| onError = continue | 首图下载失败不应阻断整条链路（文章可以没有特色图） |

**技术机理**
n8n 的 item 有 `json` 和 `binary` 两部分。`responseFormat: file` 时二进制写入 `binary.data`，含 `fileName`/`mimeType`/`fileSize` 元信息。下游上传节点用 `inputDataFieldName: data` 引用它。

**坑与经验**
- 如果首图是 403/SSL 失败，`$binary.data` 不存在，节点 14 会报 "expects binary file 'data'"。因为开了 `continueRegularOutput`，这个错误被吞掉，流程继续 → 结果是**文章没有特色图**（`featured_media: 0`）。
- 想区分"没图"和"下载失败"，需要在 14 号节点后加判断分支。

---

### 8.2 节点 14 · HTTP Request3（上传 WordPress 媒体库）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `http://host.docker.internal:8090/wp-json/wp/v2/media` |
| 方法 | `POST` |
| 认证 | Generic Credential Type → HTTP Basic Auth |
| Content-Type | `binaryData`（`inputDataFieldName: data`） |

**参数逐项**

| 参数 | 取值 | 说明 |
|---|---|---|
| URL | `.../wp/v2/media` | WordPress REST API 的媒体端点 |
| Authentication | `genericCredentialType` + `httpBasicAuth` | 用 WordPress **应用密码**（不是登录密码）做 Basic Auth |
| Header `Content-Disposition` | `attachment; filename="xxx.jpg"` | **必填**：WP 从这里的 filename 推断文件名与类型 |
| Body Content-Type | `binaryData` | 直接把 `$binary.data` 当请求体 |
| Input Data Field Name | `data` | 与节点 13 的 `$binary.data` 对应 |

**`Content-Disposition` 表达式逐段解读**

```js
={{ 'attachment; filename="' + (
      ($binary.data && $binary.data.fileName)
        ? String($binary.data.fileName).replace(/[^\w.\-]/g, '_')   // 只保留字母数字下划线点横线
        : 'featured.jpg'
   ) + '"' }}
```

- `$binary.data.fileName` 是 n8n 从 URL 或响应头推断的文件名
- **必须做字符白名单过滤**：中文名/特殊字符会让 header 编码出错，WP 直接 400
- 兜底 `featured.jpg`，避免空名

**返回**：`{ id: 123, source_url: "https://.../xxx.jpg", ... }`，其中 `id` 就是 `featured_media` 要的值。

**坑与经验**
- 端口 `8090` 是 WordPress 容器的映射端口，实际按你的 compose 调整。
- 上传前 WP 会校验 MIME 白名单；`.webp` 需要 WP 5.8+ 且服务器支持。
- 如果返回 `401`，八成是用了登录密码而不是**应用密码**。

---

### 8.3 节点 15 · Create a post（发布 WordPress 文章）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `http://host.docker.internal:8090/wp-json/wp/v2/posts` |
| 方法 | `POST` |
| Body | JSON（表达式构造） |

**jsonBody 表达式**

```js
={{ JSON.stringify({
     title: $('Code in JavaScript3').first().json.query,                  // 标题 = 用户主题
     content: $('Code in JavaScript3').first().json.brief,                // 正文 = 简报纯文本
     status: 'publish',                                                   // 直接发布（草稿用 'draft'）
     featured_media: Number($('HTTP Request3').first().json.id) || 0      // 特色图 ID，取不到则 0
   }) }}
```

**逐项说明**

| 字段 | 说明 |
|---|---|
| `title` | WP 会自动转义 HTML |
| `content` | 传**纯文本**（含 `\n`）。WP 的 Gutenberg 会按换行切段落。想要富文本得传带标签的 HTML |
| `status` | `publish` / `draft` / `private` |
| `featured_media` | 媒体库 ID。**`Number() || 0`** 是关键：取不到时给 0（无特色图），而不是 `NaN`（会让整个请求 400） |

**返回**：`{ id, link: "https://站点/?p=123", ... }`。`link` 被下游用于群卡片。

**坑与经验**
- `link` 里的域名是 WordPress 自己认为的站点地址（可能是 `localhost:8090`），所以 25 号节点会用 `WP_PUBLIC_BASE` 把 host 替换成公网域名。
- 想加分类/标签，需先查 `categories`/`tags` 的 ID，再在 body 里传数组。

---

### 8.4 节点 16 · Send an Email（发送 HTML 邮件）

| 项 | 值 |
|---|---|
| 类型 | `n8n-nodes-base.emailSend` v2.1 |
| From / To | `yangdq_lc@163.com` / `13332833833@163.com` |
| Subject | `={{ $('Code in JavaScript3').first().json.query }}` |
| HTML | `={{ $('Code in JavaScript3').first().json.htmlEmail }}` |
| onError | `continueRegularOutput` |

**它做什么**
用 SMTP 把 Code3 生成的完整 HTML（含 6 张图）发出去。

**参数逐项**

| 参数 | 说明 |
|---|---|
| From Email | 发件地址（需与 SMTP 账号一致，否则 163 会拒） |
| To Email | 收件地址，多个用逗号分隔 |
| Subject | 主题 = 用户提问 |
| Email Format | HTML（通过 `html` 字段隐式指定） |
| SMTP 凭据 | 在 n8n 凭据里配 `smtp.163.com:465`（SSL）+ **授权码**（不是邮箱密码） |

**技术机理**
`emailSend` 底层用 nodemailer。`html` 字段存在时自动按 `text/html` 发送。163 邮箱要求：
- 端口 465 + SSL，或 587 + STARTTLS
- 密码必须是"客户端授权码"

**坑与经验**
- 国内邮箱对 HTML 里的**外链图片**常做拦截（显示为裂图）。这就是为什么邮件里坚持用原图直链而不用代理——代理域名更容易被判定为可疑。
- 收件人若在手机端看，`max-width:680px` 的容器会被缩放，属正常。

---

## 9. 阶段七 · 飞书归档（节点 17–20 / 27–30）

到这里，内容已经发到邮件和 WordPress 了。这一阶段把它"沉淀"到飞书：一条多维表格记录 + 一篇云文档。

### 9.1 节点 17 · 飞书获取Token

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal` |
| 方法 | `POST` |
| Body | `{"app_id":"...","app_secret":"..."}` |

**它做什么**
用自建应用的 App ID / Secret 换一个 **tenant_access_token**（应用身份令牌），后续所有飞书 API 都靠它鉴权。

**参数逐项**

| 参数 | 说明 |
|---|---|
| URL | 固定端点，`/internal` 表示"企业内部应用" |
| Body（JSON） | `app_id` + `app_secret`。**这是全流程唯一放密钥的地方**（脱敏脚本会把它换成 `YOUR_FEISHU_APP_ID`） |
| onError | `continueRegularOutput`：token 拿不到时后续会失败，但不阻断已完成的邮件/WP 发布 |

**返回结构**

```json
{ "code": 0, "msg": "ok",
  "tenant_access_token": "t-g104au7S****",
  "expire": 7200 }
```

**技术机理**
- token 有效期 **7200 秒（2 小时）**
- 同一 App 在有效期内重复请求会返回**同一个 token**（不会重新签发）
- token 的**前缀**可以区分 App：不同 App 换出的前缀不同（本项目排障时正是靠这个发现"双 App 混乱"）

**坑与经验**
- **头号坑**：`17 飞书获取Token` 与 `21 回发获取Token` 必须是**同一个 App**。曾经 17 用旧 App、21 用新 App，导致"文档建出来了但群里打不开"（owner 是旧 App，新 App 无权改权限）。
- `app_secret` 泄露 = 别人可以以你的应用身份操作所有授权资源。所以 `scripts/sanitize_workflow.py` 会在入库前替换掉它。

---

### 9.2 节点 18 · 飞书根目录

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/drive/explorer/v2/root_folder/meta` |
| 方法 | `GET` |
| Header | `Authorization: Bearer {{ token }}` |

**它做什么**
拿到应用云空间的**根文件夹 token**，作为新建文档的父目录。

**返回**：`{ "code":0, "data": { "token": "nodcn***", "id": "...", "user_id": "..." } }`

**为什么需要它？**
飞书建文档 API 的 `folder_token` 是必填的（不填会建在"我的空间"根，某些权限模型下会失败）。显式指定应用自己的根目录，文档 owner 才明确是"这个应用"。

**坑与经验**
- 应用云空间与个人云空间是**两套**。应用建的文档，人在飞书里默认"看不到"，必须通过权限设置开放（见 9.7）。
- 根目录 token 相对稳定，但**不要硬编码**——不同 App 不同。

---

### 9.3 节点 19 · 组装搜索存档（多维表格记录体）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| onError | `continueRegularOutput` |

**它做什么**
把"网页搜索结果 + 配图"整理成飞书多维表格的 `records` 数组（每条一个 field 集合）。

**核心代码与注释**

```js
const chatInput  = ($('飞书事件解析').first().json.chatInput || '');
const query      = ($('Code in JavaScript3').first().json.query || chatInput || '未知主题');
const searchText = ($('Code in JavaScript').first().json.search_text || '');
const c3         = ($('Code in JavaScript3').first().json || {});

// 兼容两种配图结构（多图改造后 Code3 输出 imageUrls 数组）：
//   新版：c3.imageUrls = ['https://...', ...]
//   旧版：c3.images = [{ url|img_src|thumbnail, title }, ...]
let imgs = [];
if (Array.isArray(c3.images) && c3.images.length) {
  imgs = c3.images.map((im, i) => ({
    url: im.url || im.img_src || im.thumbnail || '',
    title: im.title || ('配图' + (i + 1))
  }));
} else if (Array.isArray(c3.imageUrls)) {
  imgs = c3.imageUrls.map((u, i) => ({ url: u, title: '配图' + (i + 1) }));
}

const now = (new Date()).toISOString().slice(0,16).replace('T',' ');   // "2026-10-30 11:40"
const records = [];

// 1) 网页检索结果：最多 6 条（与 07 号节点的 slice(0,6) 对应）
const parts = String(searchText).split(/\n\s*\n/).map(s=>s.trim()).filter(Boolean);
for (const p of parts.slice(0,6)) {
  records.push({ fields: {
    查询词: query, 类型:'文字结果',
    标题: p.slice(0,50),           // 飞书文本字段虽长，但标题截断更整齐
    内容或链接: p.slice(0,200),     // 超出部分飞书会存，但列表视图不友好
    来源:'SearXNG-general', 创建时间: now
  }});
}

// 2) 配图：最多 6 条（与飞书卡片展示张数一致）
for (const im of imgs.slice(0,6)) {
  const url = im.url || '';
  if (typeof url !== 'string' || !url.startsWith('http')) continue;
  const text = (im.title ? im.title + ' ' : '') + url;
  records.push({ fields: {
    查询词: query, 类型:'图片', 标题:(im.title||'配图'),
    内容或链接: text, 来源:'SearXNG-images', 创建时间: now
  }});
}

return [{ json: { records } }];
```

**字段名必须与多维表格列名**完全一致**（含空格、标点）。不一致时飞书会静默丢弃该字段（不报错），表现为"记录写进去了但某些列是空的"。

**坑与经验**
- `split(/\n\s*\n/)` 依赖 07 号节点用 `\n\n` 分隔——两处耦合。
- 时间用 `toISOString()` 是 UTC，会比北京时间早 8 小时。想用本地时间应显式加 8 小时或改用 `toLocaleString('zh-CN')`。

---

### 9.4 节点 20 · 飞书写搜索表（批量写入多维表格）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create` |
| 方法 | `POST` |
| Body | `{{ JSON.stringify($('组装搜索存档').first().json) }}` |

**它做什么**
把上一步的 `{records:[...]}` 一次性批量写入多维表格。

**参数逐项**

| 参数 | 说明 |
|---|---|
| `app_token` | 多维表格（Base）的标识，形如 `YOUR_BITABLE_APP_TOKEN` |
| `table_id` | 表标识，形如 `YOUR_BITABLE_TABLE_ID` |
| `batch_create` | 批量创建，单次**最多 500 条** |
| Body | 直接序列化 `{records:[{fields:{...}}]}` |

**返回**：`{ "code":0, "data": { "records": [...] } }`

**⚠️ 本项目踩过的大坑：403 / 91403 Forbidden**

| 现象 | 原因 | 解法 |
|---|---|---|
| `code 91403 Forbidden` | 该 Base 由**另一个 App** 创建，当前 App 不在协作者列表 | ①让同一 App 建表；②或用 owner App 把当前 App 加为协作者 |
| `code 1063004 User has no share permission` | 连协作者列表都读不了（权限全无） | 同上 |
| `code 0` 但 created=0 | 字段名不匹配，全部被丢弃 | 核对列名 |

> **实测结论**：飞书的 `permissions/{token}/members` 接口对**应用类型协作者**支持很差——`member_type: openapp` 报 `field validation failed`，`appid` 报 `Invalid parameter`。**最可靠的解法是用当前 App 自己创建 Base**（owner 就是它，天然有写权限），然后预先把字段建好。
>
> 本项目最终就是这么做的：用新 App 新建 Base `飞书存档 · 搜索记录`，建好 6 个文本字段（查询词/类型/标题/内容或链接/来源/创建时间），再指向它。

**坑与经验**
- 该节点原本指向旧 App 的 Base，**一直在静默 403**（因为开了 `continueRegularOutput`）。这类"静默失败"必须靠**回读执行数据**才能发现，不能只看工作流状态是否 success。
- 建字段时类型要选 **1（多行文本）**；如果建成"单选"，写入不在选项内的值会被拒绝。

---

### 9.5 节点 27 · 飞书建文档

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/docx/v1/documents` |
| 方法 | `POST` |

**jsonBody**

```js
={{ JSON.stringify({
     title: '简报归档 · ' + ($('飞书事件解析').first().json.chatInput||'').toString().slice(0,20),
     folder_token: $('飞书根目录').first().json.data.token
   }) }}
```

**参数逐项**

| 字段 | 说明 |
|---|---|
| `title` | 文档标题。截断 20 字避免标题过长（飞书标题栏会挤） |
| `folder_token` | 父文件夹，来自 18 号节点 |

**返回**：`{ "code":0, "data": { "document": { "document_id": "doxcnXXXX", "title": "..." } } }`

**技术机理**
飞书新版文档（docx）采用**块（block）结构**：文档由一个个 block 组成，`document_id` 同时也是根 block 的 ID。写内容要往"根 block 的 children"里插 block。

**坑与经验**
- `document_id` 与 `block_id` 同值，这解释了下个节点 URL 里出现两次同一个 ID。
- 建文档不需要先建文件夹，但需要 `docx:document` 权限。

---

### 9.6 节点 28 · 组装飞书文档（生成 block 结构）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| onError | `continueRegularOutput` |

**核心代码**

```js
// 公网基址：由 scripts/refresh_public_urls.py 自动维护（cpolar 域名每日可能变化）
const WP_PUBLIC_BASE = 'https://xxxx.r16.cpolar.top';

const query = ($('Code in JavaScript3').first().json.query || '未知主题');
const brief = ($('Code in JavaScript3').first().json.brief || '');
const htmlEmail = ($('Code in JavaScript3').first().json.htmlEmail || '');
const wp = ($('Create a post').first().json || {});
let wpLink = (wp.link || '');
if (wpLink && WP_PUBLIC_BASE) {
  // 把 WP 返回的 localhost 域名换成公网域名
  wpLink = wpLink.replace(/^https?:\/\/[^/]+/i, WP_PUBLIC_BASE.replace(/\/+$/, ''));
}

// HTML 去标签 → 纯文本摘要（给文档第二段用）
const plain = String(htmlEmail||'').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim().slice(0,800);

const blocks = { children: [
  { block_type: 2, text: { elements:[{text_run:{content:'📧 邮件主题：' + query}}], style:{} } },
  { block_type: 2, text: { elements:[{text_run:{content:'邮件正文（摘要）：' + plain}}], style:{} } },
  { block_type: 2, text: { elements:[{text_run:{content:'📝 WordPress 文章标题：' + query}}], style:{} } },
  { block_type: 2, text: { elements:[{text_run:{content:'文章正文（简报）：' + String(brief||'').slice(0,2000)}}], style:{} } },
  { block_type: 2, text: { elements:[{text_run:{content:'文章链接：' + wpLink}}], style:{} } }
] };

return [{ json: { body: blocks } }];
```

**`block_type: 2` 是什么？**

飞书 docx 的 block 类型枚举：

| block_type | 含义 |
|---|---|
| 1 | 页面（Page，根） |
| **2** | **文本（Text）** ← 用到 |
| 3–11 | 各级标题 Heading1–9 |
| 12 | 无序列表 |
| 13 | 有序列表 |
| 14 | 代码块 |
| 15 | 引用 |
| 22 | 分割线 |
| 27 | 图片 |

**坑与经验**
- `text_run.content` 是纯文本，**不支持 Markdown**。想加粗要用 `text_element_style.bold: true`。
- 每段内容长度有限制（约 10k 字符），超长要拆多个 block。
- `slice(0,2000)` 是为了控制单 block 体积。

---

### 9.7 节点 29 · 飞书写文档内容

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `.../docx/v1/documents/{document_id}/blocks/{document_id}/children` |
| 方法 | `POST` |

**它做什么**
往文档根 block 的 children 里**批量插入** block（即 28 号节点生成的 5 段文本）。

**URL 里为什么有两个 `document_id`？**

飞书 docx 的路径约定是 `documents/{doc_id}/blocks/{block_id}/children`——"往某个 block 下插子节点"。根 block 的 ID 恰好等于 `document_id`，所以要填两次。

**返回**：`{ "code":0, "data": { "children": [ {block_id...}, ... ] } }`

**坑与经验**
- 单次最多插 50 个 block，本项目只插 5 个，足够。
- 插入顺序 = 数组顺序，不会自动排序。

---

### 9.8 节点 30 · 设置文档权限

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `.../drive/v1/permissions/{document_id}/public?type=docx` |
| 方法 | `PATCH` |
| notes | 把文档设为「获得链接的人可阅读」，否则群里其他人打不开 |

**jsonBody**

```json
{
  "link_share_entity": "anyone_readable",
  "comment_entity": "anyone_can_view",
  "security_entity": "anyone_can_view",
  "share_entity": "anyone",
  "external_access": true,
  "invite_external": false
}
```

**参数逐项**

| 字段 | 取值 | 含义 |
|---|---|---|
| `link_share_entity` | `tenant_readable` → **`anyone_readable`** | 组织内可读 → **互联网上拿到链接即可读** |
| `comment_entity` | `anyone_can_view` | 谁可评论 |
| `security_entity` | `anyone_can_view` | 谁可查看安全设置 |
| `share_entity` | `anyone` | 谁可再分享 |
| `external_access` | `true` | 允许**组织外**访问（关键！只设 `anyone_readable` 但 `external_access:false`，外部仍打不开） |
| `invite_external` | `false` | 不主动邀请外部人 |

**为什么这一步不能省？**

飞书文档的 owner 是**创建它的应用**。群里的人是"人类用户"，与应用是不同主体。不做这一步，群成员点链接会看到"无权限访问"。

**这是本项目最曲折的一个坑**：

| 阶段 | 现象 | 根因 |
|---|---|---|
| 第一版 | 群里点文档"无权限" | `link_share_entity` 只到 `tenant_readable` |
| 改 anyone 后 | 仍然无权限 | `飞书获取Token` 用的是**旧 App**，而文档权限被**新 App** 的 token 修改 → 跨 App 无权 |
| 最终 | 统一新 App + `external_access:true` | ✅ |

**诊断技巧**：把 token 打印前 12 位，不同 App 前缀不同（形如 `t-g104au7S****`，同 App 前缀一致）。若同一工作流里出现两种前缀，就有双 App 问题。

**坑与经验**
- PATCH 的是 `/public`（公开设置），不是 `/members`（协作者）。
- 若返回 `1063004` 或 `403`，先确认"改权限用的 token"与"建文档用的 token"是同一个 App。
- `external_access: true` 需要企业管理员允许"对外分享"。若被禁用，会返回权限错误，此时只能退到 `tenant_readable`。

---

## 10. 阶段八 · 群内回发（节点 21–26）

这是用户**唯一能直接看到**的产出。它要解决一个核心难题：**飞书的消息图片不是 URL，而是 `image_key`**——必须先把每张图上传到飞书，拿到 key，才能拼进消息。

```
21 回发获取Token ──► 22 拆分配图 ──► 23 回发下载配图（逐张）──► 24 回发上传图片（逐张）
                                                                        │
                                                    26 发送富文本卡片 ◄── 25 组装群聊回发卡
```

### 10.1 节点 21 · 回发获取Token

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | 同 17 号节点 |
| Body | 同一套 `app_id/app_secret` |

**为什么要再换一次 token？**

因为 17 号节点的 token 已经被 20/27/29/30 号节点引用。n8n 里跨分支引用同一个节点的输出是可行的（都用 `.first()`），理论上不必重复。但**再次获取更稳妥**：

1. 语义清晰：归档用一套、回发用一套，互不干扰；
2. 若 17 号节点因重跑失败，21 号仍能独立拿到 token；
3. token 在有效期内重复获取返回同一个，**没有额外成本**。

**坑与经验**
- **必须与 17 号节点同 App**。这正是本项目"双 App 混乱"的源头之一：曾出现 17 用旧 App、21 用新 App，导致群消息发得出去、文档却打不开。

---

### 10.2 节点 22 · 拆分配图（1 → N 的分发器）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| ID | `a1b2c3d4-0010-...-000000000010` |
| **新增节点**（多图改造时加入） |

**它做什么**
把 Code3 的 `imageUrls` 数组（1 条数据含 N 个 URL）拆成 N 条 item，让后续 HTTP 节点**逐张**下载/上传。

**核心代码**

```js
const c3 = (() => {
  try { return $('Code in JavaScript3').first().json || {}; } catch (e) { return {}; }
})();

const urls = Array.isArray(c3.imageUrls)
  ? c3.imageUrls.filter(u => typeof u === 'string' && /^https?:\/\//i.test(u))
  : [];

// 无图时也要输出 1 个占位 item，避免整条回发链路中断
// （下游靠 image_key 是否存在来跳过；若这里返回 []，25 号节点根本不会执行）
if (!urls.length) {
  return [{ json: { url: '', idx: 0, total: 0, placeholder: true, referer: '' } }];
}

return urls.map((u, i) => ({
  json: {
    url: u,
    idx: i + 1,
    total: urls.length,
    placeholder: false
  }
}));
```

**为什么不能"不拆"？**

n8n 的 HTTP 节点是**逐 item 执行**的。如果给它一条"含 6 个 URL 的 item"，它只会发 1 个请求（且 URL 表达式取不到）。必须拆成 6 条 item，才有 6 次请求。

**为什么无图时要返回占位 item？**

n8n 的规则：**上游输出为空数组 → 下游节点不执行**。若不返回占位 item，`23→24→25` 全部跳过，"组装群聊回发卡"根本不运行，群里连"简报"都发不出去。

**坑与经验**
- 占位 item 的 `url: ''` 会让 23 号节点报"URL is empty"，但因为开了 `continueRegularOutput`，错误被转成普通 item 继续流下去——这是**有意设计**。
- `idx` 用于保序；`total` 便于日志排查。

---

### 10.3 节点 23 · 回发下载配图（逐张下载）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `={{ $json.url }}` |
| Response Format | `file` |
| Timeout | 20000ms |
| Retry | 2 次，间隔 1s |
| Ignore SSL Issues | ✅ `allowUnauthorizedCerts: true` |
| onError | `continueRegularOutput` |

**参数逐项**

| 参数 | 取值 | 为什么 |
|---|---|---|
| URL | `$json.url` | 逐 item 取，22 号节点已拆好 |
| Response Format | `file` | 必须二进制，才能给下步上传 |
| Timeout | 20s | 图源良莠不齐，慢站点很常见 |
| Retry | 2 次 | 瞬时抖动重试能救回一部分 |
| **Ignore SSL Issues** | 开 | **实测必需**：不少图床证书链不完整，n8n 默认校验会直接 `NodeSslError` |
| onError | continue | 单张失败不能阻断其它图 |

**关于 SSL 放开的风险评估**

只在这个"下载公开图片"的节点关闭证书校验，且下载内容仅用于**展示**（不执行、不解析成代码），风险可控。**绝不要**在调用飞书/WP API 的节点上关它——那会让 token 暴露在中间人风险下。

**⚠️ 关于防盗链的实测结论（很反直觉）**

| 尝试 | 结果 |
|---|---|
| 不带任何自定义头 | 新浪图床 ✅、搜狐 itc.cn ✅、36kr ✅；头条 ❌403 |
| 带 `Referer: 图片自身域名` | **新浪 ❌403**（它要求空 Referer）、搜狐 ✅ |
| 带浏览器 UA + Accept | 无明显改善 |

**结论**：不要加自定义 Referer。改为在**上游（32/12 号节点）剔除必 403 的域名**（`toutiaoimg.com` / `pstatp.com` / `byteimg.com`）。

**坑与经验**
- 失败 item 的 `json` 会变成 `{error:{message, name, context}}`，**没有 `binary`**。下步上传会报 "expects binary file 'data'"——同样被 `continueRegularOutput` 吞掉。
- 最终"卡片有几张图"取决于**上传成功的数量**，不是候选数量。这是设计的降级链：6 张候选 → 5 张下载成功 → 5 张上传成功 → 卡片 5 张。

---

### 10.4 节点 24 · 回发上传图片（换 image_key）

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/im/v1/images` |
| 方法 | `POST` |
| Content-Type | `multipart-form-data` |
| Timeout | 30000ms |

**multipart 参数**

| 部件 | 类型 | 值 | 说明 |
|---|---|---|---|
| `image` | **formBinaryData** | `inputDataFieldName: data` | 把 `$binary.data` 作为文件流上传 |
| `image_type` | formData | `message` | **必填**。`message`=用于发消息；`avatar`=头像 |

**返回**

```json
{ "code": 0, "data": { "image_key": "img_v3_0216v_xxxxx" } }
```

**技术机理**
飞书的消息图文（post）里，图片元素写的是 `{tag:"img", image_key:"..."}`，而不是 URL。这是为了：
1. 避免外链失效（飞书自己托管）；
2. 控制消息体积；
3. 支持鉴权访问。

`image_type` 决定存储桶与有效期：`message` 类型长期有效（实际会定期清理孤儿图）。

**坑与经验**
- 字段名必须是 `image`（不是 `file`/`media`），否则 400。
- 单张限制 **10MB**、格式限 jpg/jpeg/png/webp/gif/bmp/ico/tiff。
- `image_key` 与 App 绑定：**用 App-A 上传的 key，不能拿 App-B 的 token 发消息**（会报 key 不存在）。

---

### 10.5 节点 25 · 组装群聊回发卡（post 富文本）

| 项 | 值 |
|---|---|
| 类型 | `code` v2 |
| 代码长度 | 约 3.3k 字符 |

**它做什么**
把长简报切成 N 段、把 N 个 `image_key` 拼成飞书 post 富文本结构，输出"一条或几条待发送消息"。

**飞书 post 结构（必须理解）**

```json
{
  "zh_cn": {
    "title": "🤖 简报已生成",
    "content": [
      [ {"tag":"text","text":"第一段文字"} ],
      [ {"tag":"img","image_key":"img_v3_xxx"} ],
      [ {"tag":"text","text":"📄 飞书文档：https://..."} ]
    ]
  }
}
```

- `content` 是**二维数组**：外层 = 行，内层 = 同一行里的多个元素
- 一行放一个元素 = 独占一行；一行放多个 = 横排
- 支持的 tag：`text` / `a`（超链接）/ `at`（@人）/ `img` / `media` / `emotion`

**核心代码**

```js
const c3 = $('Code in JavaScript3').first().json || {};
const brief = (c3.brief || '').trim();
const query = c3.query || '';

// 公网基址（cpolar 每天变，由 scripts/refresh_public_urls.py 维护）
const WP_PUBLIC_BASE = 'https://xxxx.r16.cpolar.top';
let wpLink = (($('Create a post').first().json) || {}).link || '';
if (wpLink && WP_PUBLIC_BASE) {
  wpLink = wpLink.replace(/^https?:\/\/[^/]+/i, WP_PUBLIC_BASE.replace(/\/+$/, ''));
}

const docNode = $('飞书建文档').first().json || {};
const docId = (docNode.data && docNode.data.document && docNode.data.document.document_id) || '';
const docLink = docId ? ('https://www.feishu.cn/docx/' + docId) : '';
const chatId = $('飞书事件解析').first().json.chat_id || 'oc_...';

// ===== 收集全部上传成功的 image_key =====
function safeAll(name) {
  try {
    const ref = $(name);
    return (ref && typeof ref.all === 'function') ? ref.all() : [];
  } catch (e) { return []; }
}
const upItems = safeAll('回发上传图片');
const imageKeys = [];
for (const it of upItems) {
  const j = (it && it.json) || {};
  const key = (j.data && j.data.image_key) || j.image_key || '';
  if (key && !imageKeys.includes(key)) imageKeys.push(key);   // 失败项无 key，天然跳过
}
const MAX_IMAGES = 9;
while (imageKeys.length > MAX_IMAGES) imageKeys.pop();

// ===== 长文分段（飞书单条消息有大小限制）=====
const CHUNK = 3000;
function chunkText(t, size) {
  const paras = String(t || '').split(/\n+/).map((s) => s.trim()).filter(Boolean);
  if (!paras.length) return [''];
  const out = [];
  let cur = '';
  for (const para of paras) {
    if (para.length > size) {                       // 单段超长 → 硬切
      if (cur) { out.push(cur); cur = ''; }
      for (let i = 0; i < para.length; i += size) out.push(para.slice(i, i + size));
      continue;
    }
    if (!cur) { cur = para; }
    else if ((cur.length + 1 + para.length) <= size) { cur = cur + '\n' + para; }
    else { out.push(cur); cur = para; }
  }
  if (cur) out.push(cur);
  return out;
}

const parts = chunkText(brief, CHUNK);
const total = parts.length;
const items = [];

parts.forEach((seg, i) => {
  const isFirst = (i === 0);
  const isLast  = (i === total - 1);
  const blocks = [];

  const head = isFirst ? ('📌 ' + query + '\n\n') : ('（续 ' + (i + 1) + '/' + total + '）\n\n');
  blocks.push([{ tag: 'text', text: head + seg }]);

  // ===== 配图集中展示（第 1 条消息正文之后，一图一行）=====
  if (isFirst && imageKeys.length) {
    blocks.push([{ tag: 'text', text: '\n📸 配图（' + imageKeys.length + ' 张）' }]);
    for (const key of imageKeys) {
      blocks.push([{ tag: 'img', image_key: key }]);
    }
  }

  const links = [];
  if (docLink) links.push('📄 飞书文档：' + docLink);
  if (wpLink)  links.push('🌐 WordPress：' + wpLink);
  if (isLast && links.length) blocks.push([{ tag: 'text', text: links.join('\n') }]);

  const title = isFirst ? '🤖 简报已生成' : ('🤖 简报（续 ' + (i + 1) + '/' + total + '）');

  items.push({
    json: {
      chat_id: chatId,
      postContent: JSON.stringify({ zh_cn: { title, content: blocks } }),
      part: i + 1, total,
      imageCount: (isFirst ? imageKeys.length : 0),
      textLen: seg.length
    }
  });
});

return items;
```

**设计决策：为什么图放"第 1 条"，不是最后？**

| 位置 | 优点 | 缺点 |
|---|---|---|
| **第 1 条（选中方案）** | 打开群就看见图，视觉冲击强；标题+正文+图一屏内 | 正文很长时图被顶到下面 |
| 最后 1 条 | 顺着读下来像"图集" | 长文要划很多屏才看到图 |

用户选择"顶部集中"，所以放在第 1 条正文之后。

**为什么每张图单独一行（而不是一行放 2–3 张）？**

飞书 post 的 `content` 行宽自适应，一行多图会被压缩成很小的缩略图（尤其手机端）。一行一图能保持较大显示尺寸。

**`CHUNK = 3000` 的依据**

飞书 post 消息 content 的 JSON 体积上限约 30KB（UTF-8 字节）。中文 1 字 ≈ 3 字节，3000 字 ≈ 9KB，加上 image_key 等仍远低于上限，留足余量。

**坑与经验**
- `imageKeys.includes(key)` 去重是必要的：重试场景可能产生重复上传。
- `MAX_IMAGES = 9` 是硬上限：一条消息塞太多图，部分客户端会渲染不全。
- **文档 ID 引用的是 27 号节点**（飞书建文档），而它在**另一条并行分支**上。因为 n8n 会等所有分支跑完，25 执行时 27 已结束，所以能取到值。但**如果 27 失败**，这里 `docLink` 为空，卡片里就不显示文档链接——不会报错，属优雅降级。

---

### 10.6 节点 26 · 发送富文本卡片

| 项 | 值 |
|---|---|
| 类型 | `httpRequest` v4.5 |
| URL | `https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id` |
| 方法 | `POST` |
| Body | `{{ JSON.stringify({receive_id: $json.chat_id, msg_type:'post', content: $json.postContent}) }}` |

**参数逐项**

| 参数 | 说明 |
|---|---|
| `receive_id_type=chat_id` | 声明 `receive_id` 是群 ID（另有 `open_id`/`user_id`/`email`） |
| `receive_id` | 群 ID（`oc_...`） |
| `msg_type` | **`post`** = 富文本。也支持 `text`/`image`/`interactive`（卡片） |
| `content` | **是字符串化的 JSON**（不是对象）。飞书 API 里 content 永远是字符串 |

**返回**

```json
{ "code": 0, "msg": "success", "data": { "message_id": "om_x100b..." } }
```

**多 item 行为**
如果简报超长被拆成 3 条，这个节点会**执行 3 次**，群里收到 3 条消息（第 1 条带图，后 2 条是续文，最后 1 条带链接）。

**坑与经验**
- `content` 必须是 `JSON.stringify` 后的**字符串**。直接传对象会 400。
- `msg_type` 与 `content` 结构必须匹配：`post` 要 `{zh_cn:{title,content}}`，`text` 要 `{"text":"..."}`。
- 群机器人发消息需要把机器人**加进群**，否则报 `230002 bot is not in chat`。
- 若报 `99991663` 之类的错误，通常是 token 与 `chat_id` 不属于同一租户。

---

## 附录 A · 核心代码逐段解读

（正文各节点已逐段注释，此处只做"改代码前必读"的总结）

### A.1 三条铁律

| 规则 | 说明 |
|---|---|
| **1. 引用上游用 `$('节点名')`，不要用 `$input`** | `$input` 只含直接上游的数据；跨分支、跳节点时必须用节点名。多 item 场景用 `.all()`，取一条用 `.first()`，配对用 `.item` |
| **2. 代码返回值必须是 `[{json:{...}}]` 数组** | 返回裸对象会报错；返回 `undefined` 会让下游"没有数据" |
| **3. 任何"可能失败"的外部调用都配 `onError: continueRegularOutput`** | 否则一张图 403 就能让整条链路崩掉，前 60 秒的 AI 生成全部白费 |

### A.2 常见表达式速查

| 表达式 | 含义 |
|---|---|
| `{{ $json.field }}` | 当前 item 的字段 |
| `{{ $('X').first().json.f }}` | X 节点第一条输出的字段 |
| `{{ $('X').all() }}` | X 节点全部输出（数组） |
| `{{ $('X').item.json.f }}` | 与当前 item 同源的 X 输出（paired item） |
| `{{ $binary.data }}` | 当前 item 的二进制数据 |
| `{{ $now }}` / `{{ $today }}` | 当前时间（Luxon 对象） |
| `{{ $env.MY_VAR }}` | 环境变量（需 `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`） |

### A.3 Code 节点的两种运行模式

| 模式 | 何时用 | 拿到什么 |
|---|---|---|
| **Run Once for All Items** | 需要聚合/分流（本项目的 Code1/2/3） | `$input.all()` 返回全部 item |
| **Run Once for Each Item** | 逐条独立处理 | `$input.item` 是当前这一条 |

误用会导致：本该聚合的代码跑了 N 次（生成 N 份邮件），或本该逐条的代码只处理了第一条（配图只出 1 张——这正是改造前的 bug）。

---

## 附录 B · 故障排查手册

### B.1 按症状定位

| 症状 | 最可能原因 | 排查动作 |
|---|---|---|
| 群里完全没反应 | Webhook 没通 / 飞书事件没订阅 | 飞书后台看事件推送记录；`curl -X POST` 手动打 webhook |
| 群里收到"简报已生成"但没图 | 配图链路失败 | 查 `Code3.imageCount`、`回发下载配图` 的 `binary` |
| 文档链接点开"无权限" | 双 App / 权限没设 | 对比 token 前缀；查 `设置文档权限` 返回 |
| 多维表格没有新记录 | 403（App 不是 Base owner） | 查 `飞书写搜索表` 的返回 `code` |
| 简报内容和主题无关 | searxng 返回 0 条 → AI 幻觉 | 查 `07 Code` 的 `noMaterial` / `hitCount` |
| 简报被截断（JSON 解析失败） | `numPredict` 太小 | 查 `09 Code1` 的 `brief` 是否半句结尾 |
| 执行 60s+ 超时 | 正常（LLM+多图） | 不是故障；已用"先回执"规避飞书超时 |
| `index.lock` 报错 | 并发跑 git | 见 README 的同步脚本说明 |

### B.2 读执行数据的三个脚本

本仓库 `_diag/` 下有现成的排查脚本（不进 Git，仅本地用）：

| 脚本 | 用途 |
|---|---|
| `q_multiimg.js <execId>` | 一次看全：各节点 item 数、关键词、图片 URL、image_key、卡片 img 块数 |
| `q_detail.js <execId>` | 看失败 item 的具体 error（403/SSL/超时） |
| `q_search.js <execId>` | 看 searxng 返回条数、search_text 长度、AI 原始输出 |

用法（需要容器内有 `node:sqlite`，即 Node 22+）：

```bash
docker cp _diag/q_multiimg.js n8nwp-n8n:/tmp/
docker exec n8nwp-n8n node /tmp/q_multiimg.js 6279
```

### B.3 "静默失败"清单

这些错误**不会**让执行变红，只能靠回读数据发现：

| 环节 | 静默表现 | 检测点 |
|---|---|---|
| 图片下载 | 少了 1 张图 | `含二进制` 数量 < item 数 |
| 图片上传 | 少了 1 张图 | 成功 image_key 数 < item 数 |
| 多维表格 | 记录没写进去 | 返回 `code != 0` |
| 飞书文档权限 | 文档建了但打不开 | `设置文档权限` 返回 code |
| WordPress 特色图 | 文章没封面 | `featured_media: 0` |

**建议**：生产环境加一个"每日巡检"，用 `execution_entity` 里 `status='success'` 但关键指标不达标的记录做告警（见附录 D）。

---

## 附录 C · 性能、成本与容量

### C.1 实测耗时分布（execId 6279，主题「人形机器人产业化」）

| 阶段 | 耗时 | 占比 |
|---|---|---|
| 06 searxng 网页检索 | 2–5s | 5% |
| **08 AI Agent 生成 2000 字** | **30–45s** | **~60%** |
| 31 searxng 图片检索 ×2 | 2–6s | 6% |
| 23-24 下载+上传 6 张图 | 10–20s | 20% |
| 13-16 WP + 邮件 | 3–6s | 7% |
| 17-30 飞书归档（5 个 API） | 3–6s | 7% |
| **合计** | **60–90s** | 100% |

### C.2 优化方向

| 目标 | 手段 | 预期收益 |
|---|---|---|
| 缩短耗时 | 配图下载/上传并行（n8n 天然逐 item 并行度低，可拆成多个 HTTP 节点） | -10s |
| 缩短耗时 | 换更小模型（qwen3:4b） | -15s，质量下降 |
| 提高稳定性 | 图片下载换 `got`/`undici` 自定义脚本 | 成功率 +10% |
| 降成本 | 用本地 Ollama 已是零 API 成本 | — |

### C.3 资源占用

| 资源 | 量级 |
|---|---|
| Ollama 显存（qwen3:8b, 16k ctx） | 6–8 GB |
| n8n 内存峰值（6 张图二进制在内存） | +50–80 MB |
| 飞书 API 调用次数/次执行 | 约 12 次 |
| WordPress API | 2 次 |
| searxng | 3 次（1 网页 + 2 图片） |

### C.4 配额限制

| 项 | 限制 |
|---|---|
| 飞书 `im/v1/messages` | 单群 100 条/分钟（超限 429） |
| 飞书 `im/v1/images` | 单张 ≤10MB |
| 飞书 `bitable batch_create` | ≤500 条/次 |
| 飞书 `docx children` | ≤50 block/次 |
| searxng | 取决于上游引擎限流，**无 SLA** |

---

## 附录 D · 二次开发指南

### D.1 常见改造点

| 想改什么 | 改哪里 | 注意 |
|---|---|---|
| 改简报字数 | 08 提示词"约 2000 字" + 33 的 `numPredict` | **必须联动**，否则截断 |
| 改配图数量 | 09 的 `MAX_KEYWORDS` + 12 的 `MAX_PER_KEYWORD`/`TARGET_IMAGES` | 三者一起改 |
| 改配图来源 | 31 的 `categories`（`images`→`news`/`science`） | 字段结构会变，Code2 要跟着改 |
| 加群 / 换群 | 25 的 chat_id 兜底 + 飞书事件订阅 | 群 ID 形如 `oc_...` |
| 换模型 | 33 的 Model | 需先 `ollama pull` |
| 文章改草稿 | 15 的 `status: 'publish'` → `'draft'` | — |
| 邮件加附件 | 16 节点加 Attachments | 需二进制字段 |

### D.2 建议新增：失败告警分支

因为"先回执"会导致错误静默，建议在工作流**最外层**加一个错误触发器：

```
[Error Trigger] ──► [Code: 提取 executionId/节点/错误] ──► [飞书发消息给管理员]
```

n8n 的 `Error Trigger` 节点会在**任何**工作流执行失败时触发，`$json.execution` 里有完整上下文。

### D.3 建议新增：每日巡检

```
[Schedule Trigger 每天 9:00]
  ──► [Postgres/SQLite 查询最近 24h 执行]
  ──► [Code: 统计 success 率、平均耗时、异常画像]
  ──► [飞书发消息]
```

巡检要点：图片成功率（`image_key` 数 / 候选数）、多维表格写入 `code`、文档权限返回码。

### D.4 数据模型（如果要做成多租户）

当前所有配置（App 凭据、群 ID、Base ID、WP 地址）都硬编码在节点里。要做多租户需要抽出配置表：

```sql
CREATE TABLE tenant (
  id            TEXT PRIMARY KEY,
  feishu_app_id TEXT, feishu_app_secret TEXT,
  chat_id       TEXT,
  bitable_token TEXT, bitable_table TEXT,
  wp_base       TEXT, wp_user TEXT, wp_pass TEXT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

然后在 17/20/21/26 节点用 `{{ $('查询租户').first().json.feishu_app_id }}` 动态取值。

---

## 结语

这套工作流的价值不在于"用了多少节点"，而在于**每一条看似多余的判断，都是被真实故障打过之后加上去的**：

- `noMaterial` 标记 → 防 AI 幻觉跑题
- `query` 作首关键词 → 防配图跑偏
- 两轮配额 → 防"某词无图导致只有 3 张"
- `allowUnauthorizedCerts` → 防证书不全的图床
- 剔除防盗链域名 → 防必 403 的图源
- `continueRegularOutput` → 防单点失败毁掉整条链路
- 先回执再干活 → 防飞书超时重推

**排障时优先回读执行数据，而不是看"执行成功"**——本项目的多维表格曾静默 403 了很久，工作流一直显示绿色。

---

*文档随工作流版本更新。最近更新：2026-10-30（多图改造 + 多维表格迁移 + 防幻觉加固）*




