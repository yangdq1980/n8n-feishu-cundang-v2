#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将「本地运行版」工作流（含明文密钥）脱敏为「可公开同步版」。

输入：  根目录 wf8_feishu_groupbot.json   （真实密钥，已被 .gitignore 忽略）
输入：  根目录 .env                        （真实密钥，已被 .gitignore 忽略）
输出：  workflow/wf8_feishu_groupbot.json   （占位符版本，提交到 GitHub）

设计要点（安全第一）：
- 本脚本【不包含任何真实密钥】——真实值全部从本地 .env 读取。
- 只做纯文本替换，替换后仍是合法 JSON（不破坏结构）。
- 输出后做「残留扫描」，若仍能搜到任何真实值则报错退出（防止漏网）。
- 每日同步脚本会在 git add 之前调用本脚本，确保密钥永不进入 Git 历史。

用法：python scripts/sanitize_workflow.py
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "wf8_feishu_groupbot.json"
DST = ROOT / "workflow" / "wf8_feishu_groupbot.json"
ENV = ROOT / ".env"


def load_env(path: pathlib.Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def build_pairs(env: dict):
    pairs = [
        (env.get("FEISHU_APP_SECRET"), "YOUR_FEISHU_APP_SECRET"),
        (env.get("OLD_FEISHU_APP_SECRET"), "YOUR_OLD_FEISHU_APP_SECRET"),
        (env.get("FEISHU_APP_ID"), "YOUR_FEISHU_APP_ID"),
        (env.get("OLD_FEISHU_APP_ID"), "YOUR_OLD_FEISHU_APP_ID"),
        (env.get("FEISHU_CHAT_ID"), "YOUR_FEISHU_CHAT_ID"),
        # 飞书多维表格（归档目的地）：app_token / table_id 属个人资源标识，同样不应公开
        (env.get("BITABLE_APP_TOKEN"), "YOUR_BITABLE_APP_TOKEN"),
        (env.get("BITABLE_TABLE_ID"), "YOUR_BITABLE_TABLE_ID"),
    ]
    # cpolar host：从 WP_PUBLIC_BASE 提取纯域名
    wp = (env.get("WP_PUBLIC_BASE") or "").strip()
    host = re.sub(r"^https?://", "", wp).strip("/")
    if host:
        pairs.append((host, "YOUR_CPOLAR_HOST"))
    # 只保留长度足够的真实值（避免误伤短串）
    pairs = [(r, ph) for r, ph in pairs if r and len(r) >= 8]
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def main() -> int:
    if not SRC.exists():
        print(f"[err] source workflow not found: {SRC}", file=sys.stderr)
        return 1
    if not ENV.exists():
        print(f"[warn] .env not found ({ENV}); running without secret mapping", file=sys.stderr)

    pairs = build_pairs(load_env(ENV))
    text = SRC.read_text(encoding="utf-8")

    hits = []
    for real, ph in pairs:
        if real in text:
            hits.append(real)
        text = text.replace(real, ph)

    leaked = [r for r, _ in pairs if r in text]
    if leaked:
        print(f"[err] {len(leaked)} sensitive value(s) still present after sanitize", file=sys.stderr)
        return 2
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[err] sanitized JSON is invalid: {e}", file=sys.stderr)
        return 3

    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(text, encoding="utf-8")

    print(f"[ok] sanitized -> {DST}")
    print(f"[ok] replaced {len(hits)} sensitive type(s):")
    for r in hits:
        print(f"     - {r[:6]}***{r[-4:]}  ({len(r)} chars)")
    if not hits:
        print("     (0 hits: check that .env has real values)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
