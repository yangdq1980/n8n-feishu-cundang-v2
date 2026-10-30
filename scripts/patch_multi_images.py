# -*- coding: utf-8 -*-
"""
配图多图改造补丁（幂等）

改造目标
--------
1. Code in JavaScript1 : 输出「brief + 每个 AI 关键词各 1 条 image_search」
                        （原实现只取 keywords[0]，导致永远只有 1 张图）
2. Code in JavaScript2 : 逐条处理每个关键词的 searxng 结果，每关键词挑 3 张可用图
3. Code in JavaScript3 : 汇总去重、按关键词配额截断（<=3/词、<=9 总），
                        并新增 imageUrls 数组供回发链路逐张上传
4. 新增「拆分配图」节点  : 把 imageUrls 拆成 N 个 item，让后续 HTTP 节点逐张下载
5. 回发下载配图          : URL 改为 $json.url（逐 item），并加 20s 超时
6. 组装群聊回发卡        : 收集所有上传成功的 image_key，第 1 条消息底部集中展示全部配图

用法
----
    python scripts/patch_multi_images.py [workflow.json]
默认处理 wf8_feishu_groupbot.json，原地写回（先自行备份）。
"""
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------- 节点代码 --

CODE1 = r"""
const items = $input.all();
if (items.length === 0) return items;

const output = items[0].json;
// 1. 兼容多种 Ollama / Agent 返回格式
let rawText = output.text || output.output || output.content || output.message?.content || '';

// 2. 提取 JSON 内容（防止模型输出额外的废话或 markdown 代码块）
let jsonMatch = rawText.match(/[\s\S]*\{[\s\S]*\}[\s\S]*/);
if (!jsonMatch) jsonMatch = [rawText];

let parsed = {};
try {
    parsed = JSON.parse(jsonMatch[0]);
} catch (error) {
    // 3. 解析失败时用正则兜底抽取 brief / image_keywords
    const briefMatch = rawText.match(/"brief"\s*:\s*"([^"]*)"/);
    const keysMatch = rawText.match(/"image_keywords"\s*:\s*\[(.*?)\]/s);
    parsed = {
        brief: briefMatch ? briefMatch[1] : rawText,
        image_keywords: keysMatch ? keysMatch[1].split(',').map(s => s.trim().replace(/"/g, '')) : []
    };
}

const brief = parsed.brief || rawText;
const query = $('飞书事件解析')?.first()?.json?.chatInput || '未知主题';
// 上游检索是否为空（07 号节点打的标记）；节点重跑场景下取不到，做 try 兜底
let noMaterial = false;
try { noMaterial = $('Code in JavaScript')?.first()?.json?.noMaterial === true; } catch (e) { noMaterial = false; }

// 4. 关键词清洗：去空、去重、限数量
//    【关键设计】把 query（用户原始主题）作为第一个关键词。
//    原因：实测 LLM 在检索素材为空时会「幻觉」出与主题无关的词（例：问低空经济，却给出新能源汽车），
//    用主题本身兜底，能保证至少有一组配图与主题相关。
const MAX_KEYWORDS = 2;          // 2 个关键词 × 每词 3 张 = 6 张图（与产品预期一致）
const seenKw = new Set();
const keywords = [];

function addKw(k) {
    const kw = String(k == null ? '' : k).trim();
    if (!kw) return false;
    const sig = kw.toLowerCase();
    if (seenKw.has(sig)) return false;
    seenKw.add(sig);
    keywords.push(kw);
    return true;
}

// 4.1 主题本身优先（过长时截断，搜索词过长会显著降低命中率）
addKw(String(query).replace(/[，。！？、,.!?]/g, ' ').trim().slice(0, 20));

// 4.2 再补 LLM 给出的细分关键词
const rawKeywords = Array.isArray(parsed.image_keywords) ? parsed.image_keywords : [];
for (const k of rawKeywords) {
    if (keywords.length >= MAX_KEYWORDS) break;
    addKw(k);
}

const results = [];

// 5. 简报数据（供 Switch 的 brief 分支 + Merge 汇合）
results.push({
    json: {
        type: 'brief',
        query,
        brief,
        keywords,
        noMaterial
    }
});

// 6. 每个关键词各生成 1 条搜索任务
//    HTTP Request1 会逐条执行，一条任务 = 一次 searxng 图片检索 = 最多 3 张图
keywords.forEach((kw, i) => {
    results.push({
        json: {
            type: 'image_search',
            query,
            keyword: kw,
            kwIndex: i + 1,
            kwTotal: keywords.length
        }
    });
});

return results;
"""

CODE07 = r"""
// 把 searxng 的网页检索结果拼成给 LLM 的素材文本
const data = $input.first().json;
const list = Array.isArray(data.results) ? data.results : [];

const text = list.slice(0, 6).map((r, i) =>
  `【${i + 1}】${r.title || '无标题'}\n链接：${r.url || ''}\n摘要：${r.content || '（无摘要）'}`
).join('\n\n');

const trig = $('飞书事件解析').first().json;

// 【重要】检索为空的兜底
// 实测：searxng 上游引擎偶发限流会返回 0 条。若不提示，LLM 会凭空编造与主题无关的内容。
// 这里显式给出「素材不足」信号，让模型只基于主题本身作答，并把标记透传给下游（配图改用主题兜底）。
const noMaterial = text.length === 0;
const searchText = noMaterial
  ? `（本次联网检索未返回任何结果，主题为「${trig.chatInput}」。请仅依据该主题的通识知识撰写，禁止编造具体数据、机构名称与时间点；无法确认的内容一律使用定性描述。）`
  : text;

return [{
  json: {
    query: trig.chatInput,
    search_text: searchText,
    noMaterial,
    hitCount: list.length,
    sessionId: trig.chat_id || 'feishu-group'
  }
}];
"""

CODE2 = r"""
// 模式：Run Once for All Items
// 输入：HTTP Request1 对每个关键词的 searxng images 检索结果（N 个 item）
// 输出：每个关键词 1 个 image_result item，内含最多 PER_KEYWORD 张可用图
// 说明：这里多挑几张（6），是为了给 Code3 留出「某词无图时用别词补足」的余量。
const PER_KEYWORD = 6;                              // 每个关键词最多挑几张候选（下游再按配额截断）
const EXT_OK = /\.(jpe?g|png|webp|gif|bmp)(\?|#|$)/i;   // 飞书/邮件可用的图片扩展名
const EXT_BAD = /\.(svg|ico|tiff?)(\?|#|$)/i;        // 飞书 image 上传不支持的格式
const BAD_HOST = /(toutiaoimg\.com|pstatp\.com|byteimg\.com)/i;  // 强防盗链，实测必 403

// 与 HTTP Request1 的输入一一对应（同为 Code in JavaScript1 的 image_search 分支，顺序一致）
let kwItems = [];
try {
    kwItems = $('Code in JavaScript1').all().filter(i => i.json && i.json.type === 'image_search');
} catch (e) { kwItems = []; }

// 从一条 searxng 结果里挑出「最像真图片、且大概率能下载」的 URL
function pickUrl(r) {
    const cand = [r.img_src, r.thumbnail_src, r.thumbnail]
        .filter(u => typeof u === 'string' && /^https?:\/\//i.test(u));
    for (const u of cand) {
        if (!EXT_OK.test(u)) continue;
        if (EXT_BAD.test(u)) continue;
        if (BAD_HOST.test(u)) continue;
        return u;
    }
    // 退路：没有扩展名明确的候选时，取第一个非防盗链的
    // （例如搜索引擎的缩略图代理 kkimgs.yisou.com/ims?kt=url... 无扩展名但通常可下载）
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
    const kw = (kwItems[idx] && kwItems[idx].json.keyword) || (kwItems[0] && kwItems[0].json.keyword) || '';

    const images = [];
    const seenUrl = new Set();
    for (const r of list) {
        if (images.length >= PER_KEYWORD) break;
        const url = pickUrl(r);
        if (!url) continue;
        const base = url.split('?')[0];
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

    out.push({
        json: {
            type: 'image_result',
            keyword: kw,
            kwIndex: idx + 1,
            count: images.length,
            images
        }
    });
});

return out.length ? out : [{ json: { type: 'image_result', keyword: '', kwIndex: 0, count: 0, images: [] } }];
"""

CODE3 = r"""
// 模式：Run Once for All Items
// 加固：$input 可能在"节点重跑/paired-item 过滤"场景丢 item（实证执行#5774），
// 故同时按节点名兜底补拉 Merge / Code1 / Code2 的输出，按 JSON 去重。
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

// ===== 配图配额（两轮分配）=====
// 目标：2 个关键词共 6 张。
// 第一轮：每个关键词最多 3 张 —— 保证「每主题 3 张」的语义。
// 第二轮：若某关键词搜不到图导致总量不足 6 张，从其他关键词的剩余候选补足。
//         （实测 searxng 对部分细分词返回 0 图，不补会出现「只有 3 张」）
const MAX_PER_KEYWORD = 3;
const TARGET_IMAGES = 6;
const MAX_IMAGES = 9;
// 飞书 im/v1/images 与邮件客户端都不支持 svg，直接排除
const BAD_URL = /\.(svg|ico|tiff?)(\?|#|$)/i;
// 强防盗链图床：实测无论是否带 Referer 都返回 403，直接在上游剔除，避免占用配图配额
const BAD_HOST = /(toutiaoimg\.com|pstatp\.com|byteimg\.com)/i;

let brief = "";
let query = "";
const kwList = [];          // 保持出现顺序的关键词列表
const imgCandidates = [];   // { url, title, keyword }

function pushImg(url, title, keyword) {
  if (typeof url !== "string") return;
  if (!/^https?:\/\//i.test(url)) return;
  if (url.includes('.html') || url.includes('search?q') || url.includes('lemmaId=')) return;
  if (BAD_URL.test(url)) return;
  if (BAD_HOST.test(url)) return;
  imgCandidates.push({
    url,
    title: title || keyword || "相关配图",
    keyword: keyword || ""
  });
}

for (const j of pool) {
  if (j.query) query = j.query;
  if (j.brief) brief = j.brief;
  if (j.keyword && !kwList.includes(j.keyword)) kwList.push(j.keyword);

  // 兼容旧结构：单图字段
  pushImg(j.img_src, j.title, j.keyword);
  pushImg(j.thumbnail, j.title, j.keyword);

  // 新结构：Code in JavaScript2 输出的 images 数组
  if (Array.isArray(j.images)) {
    for (const im of j.images) {
      pushImg(im.img_src, im.title, im.keyword || j.keyword);
      pushImg(im.thumbnail, im.title, im.keyword || j.keyword);
    }
  }
}

// 1) 先按 base url 全局去重
const seen = new Set();
const dedup = [];
for (const c of imgCandidates) {
  const base = c.url.split("?")[0];
  if (seen.has(base)) continue;
  seen.add(base);
  dedup.push(c);
}

// 2) 第一轮：每关键词最多 MAX_PER_KEYWORD 张
const perKw = new Map();
const picked = [];
for (const c of dedup) {
  const kwKey = c.keyword || "__none__";
  const n = perKw.get(kwKey) || 0;
  if (n >= MAX_PER_KEYWORD) continue;
  perKw.set(kwKey, n + 1);
  picked.push(c);
}

// 3) 第二轮：总量不足时，用剩余候选补足（解决「某关键词 0 图」）
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

// 4) 最终上限
const uniq = picked.slice(0, MAX_IMAGES);

const keywordsText = kwList.join(' · ');

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// 邮件内图片一律用【原图直链】：images.weserv.nl 在本网络 ECONNRESET 不可达（收件人在国内同理）。
let imagesHtml = "";
for (const c of uniq) {
  imagesHtml += `
    <div style="margin:0 auto 36px;text-align:center;page-break-inside:avoid;">
      <img src="${esc(c.url)}" alt="${esc(c.title)}" style="max-width:100%;height:auto;border-radius:12px;box-shadow:0 6px 16px rgba(0,0,0,0.08);display:block;margin:0 auto;">
      <p style="font-size:13px;color:#888;margin-top:12px;font-style:italic;">📍 ${esc(c.title)}</p>
    </div>`;
}
if (!imagesHtml) imagesHtml = `<p style="color:#999;text-align:center;padding:30px;">😕 未检索到可用配图。</p>`;

const briefSafe = (brief || "本次未生成简报内容。").replace(/\n+/g, "</p><p style=\"font-size:16px;line-height:1.9;color:#444;margin:0 0 18px;text-indent:2em;\">");
const kwLine = keywordsText
  ? `<p style="font-size:15px;line-height:1.9;color:#1a1a1a;margin:0 0 18px;text-align:justify;"><strong>🔍 检索关键词：</strong>${esc(keywordsText)}</p>`
  : '';
const contentHtml = `
  <p style="font-size:16px;line-height:1.9;color:#444;margin:0 0 18px;text-indent:2em;"><strong style="color:#1a1a1a;">✨ 核心看点：</strong>${briefSafe}</p>
  ${kwLine}`;

const finalHtml = `<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:'Helvetica Neue',Arial,'PingFang SC','Microsoft YaHei',sans-serif;background:#f4f6f8;padding:40px 20px;margin:0;">
  <div style="max-width:680px;margin:0 auto;background:#fff;padding:55px 40px;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,0.05);">
    <h1 style="color:#1a1a1a;text-align:center;margin:0 0 40px;font-size:26px;">🤖 AI 搜图报告：${esc(keywordsText || "AI 自动搜图")}</h1>
    <div style="margin-bottom:40px;">${contentHtml}</div>
    <div style="border-top:1px dashed #e8e8e8;padding-top:30px;">
      <h3 style="color:#333;text-align:center;margin:0 0 30px;font-size:20px;">📸 精选配图（${uniq.length} 张）</h3>
      ${imagesHtml}
    </div>
    <div style="text-align:center;margin-top:50px;font-size:12px;color:#b0b0b0;border-top:1px solid #f0f0f0;padding-top:20px;">
      <p>本邮件由 n8n 自动化工作流自动生成</p>
      <p>🕒 ${new Date().toLocaleString()}</p>
    </div>
  </div>
</body></html>`;

const featuredImageUrl = uniq.length > 0 ? uniq[0].url : "";

return [{ json: {
  htmlEmail: finalHtml,
  featuredImageUrl,                         // 首图（兼容旧字段）
  featuredImageWeservUrl: featuredImageUrl, // 兼容旧字段名，值同原图（代理通道已弃用）
  imageUrls: uniq.map(c => c.url),          // 【新增】全部配图直链，供回发链路逐张上传
  imageCount: uniq.length,                  // 【新增】配图总数
  keywords: kwList,                         // 【新增】本次使用的关键词列表
  hasImage: uniq.length > 0,
  query: query || keywordsText || "AI 自动搜图",
  brief,
  keyword: keywordsText
} }];
"""

CODE_SPLIT = r"""
// 把 Code in JavaScript3 汇总出的 imageUrls 拆成多个 item，
// 让「回发下载配图 → 回发上传图片」逐张执行（一个 item = 一张图）。
const c3 = (() => {
  try { return $('Code in JavaScript3').first().json || {}; } catch (e) { return {}; }
})();
const urls = Array.isArray(c3.imageUrls) ? c3.imageUrls.filter(u => typeof u === 'string' && /^https?:\/\//i.test(u)) : [];

// 防盗链：部分图床（如今日头条 toutiaoimg）会校验 Referer，缺失直接 403。
// 这里按图片自身域名算一个 origin 作为 Referer，命中率最高。
function originOf(u) {
  try { return new URL(u).origin + '/'; } catch (e) { return ''; }
}

// 无图时也要输出 1 个占位 item，避免整条回发链路中断（下游靠 image_key 是否存在来跳过）
if (!urls.length) {
  return [{ json: { url: '', idx: 0, total: 0, placeholder: true, referer: '' } }];
}

return urls.map((u, i) => ({
  json: {
    url: u,
    idx: i + 1,
    total: urls.length,
    placeholder: false,
    referer: originOf(u)
  }
}));
"""

CODE_CARD = r"""
// 群内直发全文：不截断正文；超长按段落自动拆成多条 post 消息
// 第 1 条 = 标题 + 正文首段 + 全部配图（集中展示）
// 最后 1 条 = 飞书文档 / WordPress 链接
const c3 = $('Code in JavaScript3').first().json || {};
const brief = (c3.brief || '').trim();
const query = c3.query || '';
// ↓↓↓ 公网基址：由本补丁从本地 .env 注入（脚本自身不硬编码真实域名）
const WP_PUBLIC_BASE = '__WP_PUBLIC_BASE__';
let wpLink = (($('Create a post').first().json) || {}).link || '';
if (wpLink && WP_PUBLIC_BASE) {
  wpLink = wpLink.replace(/^https?:\/\/[^/]+/i, WP_PUBLIC_BASE.replace(/\/+$/, ''));
}
const docNode = $('飞书建文档').first().json || {};
const docId = (docNode.data && docNode.data.document && docNode.data.document.document_id) || '';
const docLink = docId ? ('https://www.feishu.cn/docx/' + docId) : '';
const chatId = $('飞书事件解析').first().json.chat_id || '__FEISHU_CHAT_ID__';

// ===== 收集全部上传成功的 image_key =====
// 「回发上传图片」逐张执行，失败的 item 只有 error 字段、没有 image_key，天然被跳过
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
  if (key && !imageKeys.includes(key)) imageKeys.push(key);
}
const MAX_IMAGES = 9;
while (imageKeys.length > MAX_IMAGES) imageKeys.pop();

// 单条 post 的正文上限（字）。飞书单条消息内容有大小限制，按 3000 字留足余量
const CHUNK = 3000;

function chunkText(t, size) {
  const paras = String(t || '').split(/\n+/).map((s) => s.trim()).filter(Boolean);
  if (!paras.length) return [''];
  const out = [];
  let cur = '';
  for (const para of paras) {
    // 单段就超长时，硬切
    if (para.length > size) {
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
  const isLast = (i === total - 1);
  const blocks = [];

  const head = isFirst
    ? ('📌 ' + query + '\n\n')
    : ('（续 ' + (i + 1) + '/' + total + '）\n\n');
  blocks.push([{ tag: 'text', text: head + seg }]);

  // ===== 配图集中展示（第 1 条消息正文之后，一图一行） =====
  if (isFirst && imageKeys.length) {
    blocks.push([{ tag: 'text', text: '\n📸 配图（' + imageKeys.length + ' 张）' }]);
    for (const key of imageKeys) {
      blocks.push([{ tag: 'img', image_key: key }]);
    }
  }

  const links = [];
  if (docLink) links.push('📄 飞书文档：' + docLink);
  if (wpLink) links.push('🌐 WordPress：' + wpLink);
  if (isLast && links.length) blocks.push([{ tag: 'text', text: links.join('\n') }]);

  const title = isFirst
    ? '🤖 简报已生成'
    : ('🤖 简报（续 ' + (i + 1) + '/' + total + '）');

  items.push({
    json: {
      chat_id: chatId,
      postContent: JSON.stringify({ zh_cn: { title, content: blocks } }),
      part: i + 1,
      total,
      imageCount: (isFirst ? imageKeys.length : 0),
      textLen: seg.length
    }
  });
});

return items;
"""

CODE_ARCHIVE = r"""
const chatInput = ($('飞书事件解析').first().json.chatInput || '');
const query = ($('Code in JavaScript3').first().json.query || chatInput || '未知主题');
const searchText = ($('Code in JavaScript').first().json.search_text || '');
const c3 = ($('Code in JavaScript3').first().json || {});

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

const now = (new Date()).toISOString().slice(0,16).replace('T',' ');
const records = [];

// 1) 网页检索结果：最多 6 条
const parts = String(searchText).split(/\n\s*\n/).map(s=>s.trim()).filter(Boolean);
for (const p of parts.slice(0,6)) {
  records.push({ fields: { 查询词: query, 类型:'文字结果', 标题: p.slice(0,50), 内容或链接: p.slice(0,200), 来源:'SearXNG-general', 创建时间: now } });
}

// 2) 配图：最多 6 条（与飞书卡片展示张数一致）
for (const im of imgs.slice(0,6)) {
  const url = im.url || '';
  if (typeof url !== 'string' || !url.startsWith('http')) continue;
  const text = (im.title ? im.title + ' ' : '') + url;
  records.push({ fields: { 查询词: query, 类型:'图片', 标题: (im.title||'配图'), 内容或链接: text, 来源:'SearXNG-images', 创建时间: now } });
}

return [{ json: { records } }];
"""

# ------------------------------------------------------------------ 节点 ID --

ID_CODE1 = "3d475114-f351-4281-a71d-b0ac5c4fda0f"
ID_CODE2 = "8bf813f1-8b28-477d-b6a9-5e81076a0243"
ID_CODE3 = "fa3fe919-86a2-4913-b3e8-2fced7aff4f9"
ID_CARD = "a1b2c3d4-0009-4a1a-9a1a-000000000009"
ID_DOWNLOAD = "a1b2c3d4-0007-4a1a-9a1a-000000000007"
ID_ARCHIVE = "fe6fc556-2207-4259-9ba0-eec6b13fd08f"
ID_SPLIT = "a1b2c3d4-0010-4a1a-9a1a-000000000010"
ID_CODE07 = "afee8663-07f5-4add-a58a-871614ae5faa"
ID_HTTP_GENERAL = "6d62f73a-9cb3-4025-97ff-0654f56dc00f"
ID_HTTP_IMAGES = "60422a91-2b33-443a-897b-3cdba470c09c"
NAME_SPLIT = "拆分配图"
NAME_TOKEN = "回发获取Token"
NAME_DOWNLOAD = "回发下载配图"

ENV_VALUES = {}


def set_retry(node, tries=2, wait=1200):
    """给节点开失败重试：searxng/图片站偶发超时与限流，重试一次能显著提高成功率。"""
    node["retryOnFail"] = True
    node["maxTries"] = tries
    node["waitBetweenTries"] = wait


def load_env():
    """从本地 .env 读取真实值（脚本里不保存任何密钥/ID）。"""
    env = {}
    p = Path(__file__).resolve().parents[1] / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fill_card(env):
    """把 CODE_CARD 里的占位符替换成 .env 中的真实值。"""
    code = CODE_CARD
    code = code.replace("__WP_PUBLIC_BASE__", env.get("WP_PUBLIC_BASE", "https://your-tunnel.example.com"))
    code = code.replace("__FEISHU_CHAT_ID__", env.get("FEISHU_CHAT_ID", "oc_your_chat_id"))
    return code


def find(nodes, node_id):
    for n in nodes:
        if n.get("id") == node_id:
            return n
    raise KeyError(node_id)


def main():
    global ENV_VALUES
    ENV_VALUES = load_env()

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "wf8_feishu_groupbot.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    nodes = doc["nodes"]
    conns = doc["connections"]

    # 1~3. 替换三段 Code 逻辑
    find(nodes, ID_CODE1)["parameters"]["jsCode"] = CODE1
    find(nodes, ID_CODE2)["parameters"]["jsCode"] = CODE2
    find(nodes, ID_CODE3)["parameters"]["jsCode"] = CODE3
    find(nodes, ID_CARD)["parameters"]["jsCode"] = fill_card(ENV_VALUES)
    find(nodes, ID_ARCHIVE)["parameters"]["jsCode"] = CODE_ARCHIVE
    find(nodes, ID_CODE07)["parameters"]["jsCode"] = CODE07

    # 检索节点加固：searxng 偶发限流/超时，重试 + 拉长超时 + 扩大图片候选量
    hg = find(nodes, ID_HTTP_GENERAL)
    hg["parameters"].setdefault("options", {})["timeout"] = 20000
    set_retry(hg, tries=2, wait=1500)

    hi = find(nodes, ID_HTTP_IMAGES)
    hi["parameters"].setdefault("options", {})["timeout"] = 20000
    set_retry(hi, tries=2, wait=1200)
    # 候选从 5 提到 8：多给下游挑选余地，提高「每关键词 3 张」的达成率
    for q in hi["parameters"]["queryParameters"]["parameters"]:
        if q.get("name") == "num_results":
            q["value"] = "8"

    # 4. 回发下载配图：改为逐 item 取 $json.url，并加超时
    #    图片源站常有证书链不完整（自签/缺中间证书），n8n 默认校验会直接失败（NodeSslError）。
    #    这里只对「下载图片」这一处放开证书校验：图片仅用于展示，不执行、不可信内容风险可控。
    dl = find(nodes, ID_DOWNLOAD)
    dl["parameters"]["url"] = "={{ $json.url }}"
    dl["parameters"].setdefault("options", {})
    opts = dl["parameters"]["options"]
    opts["response"] = {"response": {"responseFormat": "file"}}
    opts["timeout"] = 20000
    opts["allowUnauthorizedCerts"] = True
    # 防盗链说明（实测结论，勿轻易改）：
    #   1) 新浪图床(wx*.sinaimg.cn) 要求【空 Referer】，主动加 Referer 会 403；
    #   2) 头条图床(toutiaoimg/pstatp) 无论加不加都 403，只能在上游剔除。
    # 所以这里【不设自定义请求头】，只在 Code3 里过滤必 403 的域名。
    dl["parameters"]["sendHeaders"] = False
    dl["parameters"].pop("headerParameters", None)
    dl["onError"] = "continueRegularOutput"
    # 瞬时网络抖动重试 2 次，提高「6 张图都拿到」的命中率
    dl["retryOnFail"] = True
    dl["maxTries"] = 2
    dl["waitBetweenTries"] = 1000

    # 5. 新增「拆分配图」节点（幂等：已存在则复用）
    split = None
    for n in nodes:
        if n.get("id") == ID_SPLIT:
            split = n
            break
    if split is None:
        # 位置取「回发获取Token」与「回发下载配图」之间
        try:
            tok_pos = find(nodes, [n["id"] for n in nodes if n.get("name") == NAME_TOKEN][0])["position"]
            dl_pos = dl["position"]
            pos = [(tok_pos[0] + dl_pos[0]) / 2.0, (tok_pos[1] + dl_pos[1]) / 2.0]
        except Exception:
            pos = [0, 0]
        split = {
            "parameters": {"jsCode": CODE_SPLIT},
            "id": ID_SPLIT,
            "name": NAME_SPLIT,
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": pos,
        }
        nodes.append(split)
    else:
        split["parameters"]["jsCode"] = CODE_SPLIT

    # 6. 重接连接：回发获取Token → 拆分配图 → 回发下载配图
    conns[NAME_TOKEN] = {"main": [[{"node": NAME_SPLIT, "type": "main", "index": 0}]]}
    conns[NAME_SPLIT] = {"main": [[{"node": NAME_DOWNLOAD, "type": "main", "index": 0}]]}

    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[ok] patched ->", path)
    print("[ok] nodes =", len(nodes))
    print("[ok] split node id =", ID_SPLIT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
