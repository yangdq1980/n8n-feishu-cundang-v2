# -*- coding: utf-8 -*-
"""
搜索质量改造补丁（幂等）：
1. 把「HTTP Request」从 searxng general（实测 100% 返回 0 条）改为直接抓 Bing 搜索页
2. 把「Code in JavaScript」改为解析 Bing HTML，提取标题/链接/摘要，带来源编号
3. 重写 AI Agent 提示词：强制来源标注 + 禁止编造 + Markdown 结构 + 配图关键词质量约束

用法：python scripts/patch_search_quality.py [工作流路径]
"""
import json
import re
import sys
from pathlib import Path

# ------------------------------------------------------------------ 节点 ID --
ID_HTTP_SEARCH = "6d62f73a-9cb3-4025-97ff-0654f56dc00f"   # HTTP Request（general 搜索）
ID_CODE_SEARCH = "afee8663-07f5-4add-a58a-871614ae5faa"   # Code in JavaScript（素材组装）
ID_AI_AGENT = None                                        # 运行时按 name 定位

NAME_AI_AGENT = "AI Agent"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ------------------------------------------------------- Code：解析 Bing HTML --
CODE_SEARCH = r"""
// ============================================================
// Bing 搜索结果解析 + 相关性择优
//
// 背景一：searxng 的 general 引擎在当前环境已全部失效
//         （baidu/quark 被 CAPTCHA 拦截、sogou 解析异常、bing 解析器返回 0 条），
//         导致上游 search_text 恒为空，LLM 只能凭训练记忆编造内容。
//         因此改为直接抓 Bing 页面并在此解析。
//
// 背景二（关键）：Bing 中文检索对「长查询」会做宽松 OR 匹配，短的高频词
//         会压过完整词，导致严重跑题。实测：
//           查「固态电池产业化与装车进度」 -> 返回的却是「固态硬盘」
//           查「人形机器人产业化进展」     -> 返回的却是「人形娃娃/公仔」
//         而查核心词「人形机器人」则 10/10 全部相关。
//         因此这里用「多个候选查询词并发检索 + 相关性打分」自动择优。
// ============================================================

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// n8n 的 responseFormat=text 会把响应体放进 $json.data（不同版本可能在 body/text）
// 上游开启 continueRegularOutput，失败时也会往下传，因此这里必须做空值保护
let html = '';
try {
  const first = $input.first();
  const raw = (first && first.json) || {};
  html = String(raw.data || raw.body || raw.text || raw.html || '');
} catch (e) {
  html = '';
}

// ---------------------------------------------------------------- 工具函数 --
function stripTags(s) {
  return String(s == null ? '' : s)
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

// Bing 的结果链接常包一层跳转壳：https://www.bing.com/ck/a?...&u=a1<base64url>
// 不还原会导致所有链接都指向 bing 自己，简报里出现一堆无效来源。
function decodeBingHref(href) {
  if (!href) return '';
  if (href.indexOf('/ck/a?') === -1) return href;
  const m = href.match(/[?&]u=([^&]+)/);
  if (!m) return href;
  let v = decodeURIComponent(m[1]);
  if (v.indexOf('a1') === 0) v = v.slice(2);
  try {
    // Node 环境：用 Buffer 做 base64url 解码
    const pad = v + '='.repeat((4 - (v.length % 4)) % 4);
    return Buffer.from(pad, 'base64').toString('utf8');
  } catch (e) {
    try {
      return atob(v.replace(/-/g, '+').replace(/_/g, '/'));
    } catch (e2) {
      return href;
    }
  }
}

// -------------------------------------------------------- 提取 b_algo 条目 --
function parseBing(htmlText) {
  const out = [];
  if (!htmlText) return out;
  // 每个结果项形如 <li class="b_algo ..."> ... </li>
  const blocks = String(htmlText).split(/<li[^>]*class="[^"]*b_algo/).slice(1);

  for (const b of blocks) {
    if (out.length >= 12) break;                 // 最多取 12 条，够用且避免素材过长
    const seg = b.slice(0, 6000);                // 单条截断，避免正则在大段 HTML 上回溯

    const hm = seg.match(/<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/i);
    if (!hm) continue;

    const u = decodeBingHref(hm[1]);
    const t = stripTags(hm[2]);
    if (!u || !t) continue;
    if (u.indexOf('bing.com') !== -1) continue;  // 丢弃仍在 bing 站内的链接

    // 摘要：b_algo 内第一个 <p>
    const pm = seg.match(/<p[^>]*>([\s\S]*?)<\/p>/i);
    out.push({ title: t, url: u, content: pm ? stripTags(pm[1]) : '' });
  }
  return out;
}

// ------------------------------------------------- 候选查询词 + 相关性打分 --
// 生成候选：切分片段 + 逐步缩短的前缀。
// 原理：Bing 对长查询会退化，越接近「核心实体词」召回越准，因此要试多个粒度。
// 实测关键：查「固态电池」完全正确，查「固态电池 产业化」却全变成「固态硬盘」；
//           所以短词必须保留（曾经因为截断把最关键的 4 字核心词砍掉而失效）。
function genCandidates(topic) {
  const t = String(topic || '').trim();
  if (!t) return [];
  const set = new Set();
  const push = function (x) {
    x = String(x || '').trim();
    if (x.length >= 2) set.add(x);
  };

  // 按空格与中英文标点切分（切出来的片段常就是核心实体）
  const parts = t.split(/[\s,，。;；、:：\-—_/|]+/).filter(function (x) { return x.length >= 2; });
  parts.sort(function (a, b) { return b.length - a.length; });
  parts.forEach(push);

  // 前缀切片：从长到短都要覆盖到，尤其要覆盖 4 字左右的核心词
  for (const n of [10, 8, 6, 5, 4, 3]) {
    if (t.length > n) push(t.slice(0, n));
  }

  // 原句已在首轮搜过，这里排除，避免重复请求
  const arr = Array.from(set).filter(function (x) { return x !== t; });

  // 短词优先：实测短核心词召回质量显著高于长查询，先试可更快命中并提前退出
  arr.sort(function (a, b) { return a.length - b.length; });
  return arr.slice(0, 6);
}

// 相关性打分：用「主题 2-gram」与「标题+摘要」的重叠率。
// 比单字匹配更严格，能区分「固态电池」与「固态硬盘」这种一字之差。
function relevanceScore(items, topic) {
  const t = String(topic || '').replace(/[\s,，。;；、:：\-—_/|]+/g, '');
  if (!t || !items.length) return 0;
  const grams = [];
  for (let i = 0; i < t.length - 1; i++) grams.push(t.slice(i, i + 2));
  if (!grams.length) return 0;

  let total = 0;
  const top = items.slice(0, 6);
  for (const it of top) {
    const txt = String(it.title || '') + ' ' + String(it.content || '');
    let hit = 0;
    for (const g of grams) {
      if (txt.indexOf(g) !== -1) hit++;
    }
    total += hit / grams.length;
  }
  return total / top.length;   // 0~1，越高越相关
}

// --------------------------------------------------------------- 主流程 --
const topic0 = (function () {
  try { return $('飞书事件解析').first().json.chatInput || ''; } catch (e) { return ''; }
})();

let items = parseBing(html);
let usedQuery = '';
try { usedQuery = $input.first().json.query || ''; } catch (e) {}

let bestScore = relevanceScore(items, topic0);

// 首轮结果相关性太差（<0.15）时，用候选词重新检索并择优。
// 阈值说明：实测跑题素材（固态硬盘 vs 固态电池）得分约 0.02~0.08，
//          相关素材普遍 > 0.25，因此 0.15 是安全的分界线。
if (topic0 && bestScore < 0.15) {
  const cands = genCandidates(topic0);
  for (const q of cands) {
    if (q === usedQuery) continue;
    let html2 = '';
    try {
      // n8n Code 节点内置的 HTTP helper（无需在画布上额外加节点）
      const resp = await this.helpers.httpRequest({
        method: 'GET',
        url: 'https://cn.bing.com/search',
        qs: { q: q, setlang: 'zh-CN' },
        headers: {
          'User-Agent': UA,
          'Accept-Language': 'zh-CN,zh;q=0.9',
          'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'
        },
        timeout: 15000
      });
      html2 = typeof resp === 'string' ? resp : (resp && (resp.body || resp.data)) || '';
    } catch (e) {
      continue;                                  // 单个候选失败不影响整体
    }
    const cand = parseBing(String(html2));
    if (!cand.length) continue;
    const s = relevanceScore(cand, topic0);
    if (s > bestScore) {
      bestScore = s;
      items = cand;
      usedQuery = q;
    }
    if (bestScore > 0.4) break;                  // 已经足够好，提前结束省时间
  }
}

// ------------------------------------------------------------- 组装素材文本 --
const text = items
  .map(function (r, i) {
    return '【' + (i + 1) + '】' + r.title +
      '\n链接：' + r.url +
      '\n摘要：' + (r.content || '（无摘要）');
  })
  .join('\n\n');

// 事件解析节点理论上必存在，仍加保护避免整条链路因异常中断
let trig = {};
try {
  trig = $('飞书事件解析').first().json || {};
} catch (e) {
  trig = {};
}
const topic = trig.chatInput || '未知主题';

// 检索为空的兜底：明确告知 LLM 没有素材，禁止编造具体数据
const noMaterial = items.length === 0;
// 三档素材质量：
//   A. 一条都没有        -> 明确告知无素材
//   B. 有但相关性过低    -> 告知素材可能跑题，允许参考但不得照抄
//   C. 正常              -> 直接使用
const LOW_RELEVANCE = 0.15;
let searchText;
if (noMaterial) {
  searchText = '（本次联网检索未返回任何结果，主题为「' + topic +
    '」。请仅依据该主题的通识知识撰写：禁止编造具体数据、机构名称与时间点；' +
    '无法确认的内容一律使用定性描述，并在文中说明"当前未检索到公开数据源"。）';
} else if (bestScore < LOW_RELEVANCE) {
  searchText = '（注意：以下检索素材与主题「' + topic + '」的相关性很低（得分 ' +
    Math.round(bestScore * 100) / 100 + '），可能已经跑题。' +
    '你可以参考其中的通识表述，但严禁照抄与主题无关的内容，' +
    '也不要把素材里的其他领域数据写进简报。' +
    '如素材确实无用，请直接基于主题本身的通识知识撰写，并避免编造具体数据。）\n\n' + text;
} else {
  searchText = text;
}

// 域名分布（便于事后核查素材质量）
// 注意：n8n Code 沙箱里 new URL() 不可用（实测抛异常导致结果为空），改用正则提取
const domains = {};
for (const r of items) {
  const m = String(r.url || '').match(/^https?:\/\/([^/?#]+)/i);
  if (!m) continue;
  const h = m[1].replace(/^www\./, '');
  domains[h] = (domains[h] || 0) + 1;
}

return [{
  json: {
    query: topic,
    searchQuery: usedQuery,                      // 实际用于检索的查询词（可能已自动收窄）
    relevance: Math.round(bestScore * 100) / 100, // 素材与主题的相关性得分，便于事后核查
    search_text: searchText,
    noMaterial: noMaterial,
    hitCount: items.length,
    domains: Object.keys(domains).join(', '),
    sources: items,                              // 结构化来源，供下游（邮件/文档）可选引用
    sessionId: trig.chat_id || 'feishu-group'
  }
}];
"""

# ------------------------------------------------------------- AI 提示词 --
# 用三引号定义，避免提示词内的引号与 Python 字符串定界符冲突
PROMPT_AI = """=你是资深产业分析师。请基于下面的【检索素材】写一份中文深度简报。

## 素材使用规则（最高优先级，违反即为失败）
1. 简报中每一个具体数字、年份、百分比、机构/公司名称，必须能在检索素材中找到对应依据，并在该句末尾用 [n] 标注来源编号（n 为素材条目的编号）。
2. 素材中没有的数据一律禁止编造。需要表达趋势时改用定性描述（如「多数厂商」「整体呈上升态势」），或明确写「当前公开数据有限」。
3. 严禁出现素材中不存在的公司名、产品名、研究报告名、人名。

## 结构要求
1. 使用 Markdown 二级标题（## ）分为 6 个小节。
2. 每节正文不少于 300 字，整体正文约 2000 字。
3. 每节至少标注 2 处 [n] 来源。
4. 不写「总结」「结语」类收尾段落，写满第 6 节即结束。

## 配图关键词（重要）
提炼 2 个中文关键词，用于后续图片检索，要求：
- 第 1 个 = 用户主题本身或其核心实体（如「人形机器人」），必须是名词实体；
- 第 2 个 = 该主题的具象细分（具体产品、应用场景或技术名词，如「工业机械臂」）；
- 禁止使用「概念图」「示意图」「图片」「素材」等无法检索出实拍图的泛词。

## 输出格式
必须是严格 JSON，不要 markdown 代码块，不要任何解释文字：
{"brief": "简报正文（Markdown 格式，小节用 ## 标题，段落间用\\n换行）", "image_keywords": ["关键词1", "关键词2"]}

## 检索素材
{{ $json.search_text }}
"""


def find(nodes, node_id):
    for n in nodes:
        if n.get("id") == node_id:
            return n
    raise KeyError("未找到节点 id=" + node_id)


def find_by_name(nodes, name):
    for n in nodes:
        if n.get("name") == name:
            return n
    raise KeyError("未找到节点 name=" + name)


def patch(path):
    p = Path(path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    nodes = doc["nodes"]
    changed = []

    # ---- 1. HTTP Request：searxng -> 直接抓 Bing ----
    http = find(nodes, ID_HTTP_SEARCH)
    prm = http["parameters"]
    prm["url"] = (
        "=https://cn.bing.com/search?q={{ encodeURIComponent($json.chatInput) }}"
        "&setlang=zh-CN&ensearch=0&count=20"
    )
    prm.pop("queryParameters", None)          # 参数已并入 URL，避免重复
    prm["sendHeaders"] = True
    prm["headerParameters"] = {
        "parameters": [
            {"name": "User-Agent", "value": UA},
            {"name": "Accept-Language", "value": "zh-CN,zh;q=0.9"},
            {
                "name": "Accept",
                "value": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
        ]
    }
    prm["options"] = {
        "timeout": 25000,
        "response": {"response": {"responseFormat": "text"}},
        "redirect": {"redirect": {"followRedirects": True, "maxRedirects": 5}},
    }
    http["retryOnFail"] = True
    http["maxTries"] = 2
    http["waitBetweenTries"] = 1500
    http["onError"] = "continueRegularOutput"
    http["notes"] = "直接抓 Bing 结果页（searxng general 引擎已失效）；返回 HTML 由下游 Code 解析"
    changed.append("HTTP Request -> Bing 直连")

    # ---- 2. Code in JavaScript：解析 Bing HTML ----
    code = find(nodes, ID_CODE_SEARCH)
    if code["parameters"].get("jsCode", "").strip() != CODE_SEARCH.strip():
        code["parameters"]["jsCode"] = CODE_SEARCH
        changed.append("Code in JavaScript -> Bing HTML 解析")

    # ---- 3. AI Agent 提示词 ----
    agent = find_by_name(nodes, NAME_AI_AGENT)
    if agent["parameters"].get("text") != PROMPT_AI:
        agent["parameters"]["text"] = PROMPT_AI
        changed.append("AI Agent 提示词 -> 来源标注/禁编造/Markdown")

    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "wf8_feishu_groupbot.json"
    if not Path(target).exists():
        print("[err] 找不到工作流：" + target)
        return 1
    changed = patch(target)
    if not changed:
        print("[ok] 已是最新状态，无需修改")
    else:
        print("[ok] 已改造 -> " + target)
        for c in changed:
            print("     - " + c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
