#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_audit.py —— 记忆体检 / 自遗忘引擎 v0（2026-11-14 自主时间实验）

做什么：
  1. 扫记忆库，按「· 最后提及 YYYY-MM-DD」判断每条记忆多久没被提起
  2. 分类：⭐常青 / 沉睡(≥30天) / 新鲜 / 无日期标记
  3. 把「沉睡候选」交给**本地小模型**（Bonsai 27B，零 API 成本）逐条生成
     一句话摘要 + 处置建议（归档 / 保留 / 合并）
  4. 输出 Markdown 报告到 产出文档/

用法：
  python memory_audit.py                # 全量体检，写报告
  python memory_audit.py --no-llm       # 不调用本地模型，只看统计
  python memory_audit.py --days 30      # 沉睡阈值
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path

# ---- 2026-11-17 按需拉起本地模型（游戏时延后，空闲自动停）----
def _ensure_model() -> None:
    """确保本地模型可用；显存被游戏占用时直接抛错跳过，不要死等。"""
    try:
        import sys as _sys
        from _paths import TOOLS_DIR  # noqa: E402
        _sys.path.insert(0, str(TOOLS_DIR))
        import model_gate  # noqa: WPS433
        rc = model_gate.ensure()
        if rc == 3:
            raise RuntimeError("显存被占用（在玩游戏），本地模型本次跳过")
        model_gate.touch()
    except RuntimeError:
        raise
    except Exception:
        pass  # gate 本身坏了不该拖垮主流程



HOME = Path.home()
MEM = HOME / ".dsh" / "memory"
AGENTS = HOME / ".dsh" / "AGENTS.md"
from _paths import REPORTS_DIR as OUT_DIR  # noqa: E402
HITLOG = HOME / ".dsh" / "cache" / "memory-hits.jsonl"   # 检索器写的「谁被引用过」
ENGINE = "http://127.0.0.1:8095/v1/chat/completions"
MODEL = "bonsai27b"

MARK = re.compile(r"最后提及\s*(\d{4}-\d{2}-\d{2})")
EVERGREEN = re.compile(r"⭐\s*常青")


def collect() -> list[dict]:
    items = []
    files = sorted(MEM.glob("*.md")) + ([AGENTS] if AGENTS.exists() else [])
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            m = MARK.search(s)
            items.append({
                "file": f.name,
                "line": i + 1,
                "text": s,
                "date": m.group(1) if m else None,
                "evergreen": bool(EVERGREEN.search(s)),
            })
    return items


def classify(items: list[dict], days: int) -> dict:
    today = date.today()
    buckets = {"evergreen": [], "sleeping": [], "fresh": [], "undated": []}
    for it in items:
        if it["evergreen"]:
            buckets["evergreen"].append(it)
            continue
        if not it["date"]:
            buckets["undated"].append(it)
            continue
        try:
            d = datetime.strptime(it["date"], "%Y-%m-%d").date()
        except ValueError:
            buckets["undated"].append(it)
            continue
        it["age"] = (today - d).days
        (buckets["sleeping"] if it["age"] >= days else buckets["fresh"]).append(it)
    return buckets


def ask_local(prompt: str, max_tokens: int = 220) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    _ensure_model()
    req = urllib.request.Request(ENGINE, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()


def summarize_sleeping(sleeping: list[dict], batch: int = 6) -> list[str]:
    """把沉睡条目分批交给本地模型，让它逐条给「摘要 | 建议」。"""
    out = []
    for i in range(0, len(sleeping), batch):
        chunk = sleeping[i:i + batch]
        listing = "\n".join(f"{j+1}. {it['text'][:220]}" for j, it in enumerate(chunk))
        prompt = (
            "下面是我记忆库里的几条旧记忆。请对每一条输出一行，格式严格为：\n"
            "序号 | 这条在讲什么（≤20字） | 处置建议（归档/保留/合并，选一个）\n"
            "只输出这些行，不要解释。\n\n" + listing
        )
        try:
            text = ask_local(prompt)
            out.extend([ln.strip() for ln in text.splitlines() if ln.strip()][:len(chunk)])
        except Exception as e:
            out.append(f"（本地模型调用失败：{e}）")
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    items = collect()
    b = classify(items, a.days)
    today = date.today().isoformat()

    lines = [f"# 记忆体检 · {today}", "",
             f"> 由 `memory_audit.py` 自动生成；沉睡阈值 **{a.days} 天**；"
             f"摘要与建议由**本地 Bonsai 27B** 生成（零 API 成本）", ""]
    lines += ["## 总览", "", "| 类别 | 条数 | 含义 |", "|---|---:|---|",
              f"| ⭐ 常青 | **{len(b['evergreen'])}** | 永不遗忘 |",
              f"| 沉睡 ≥{a.days} 天 | **{len(b['sleeping'])}** | 遗忘候选 |",
              f"| 新鲜 | {len(b['fresh'])} | {a.days} 天内被提及 |",
              f"| 无日期标记 | {len(b['undated'])} | 没按 V1.1 规则打日期 |", ""]

    if b["sleeping"]:
        lines += ["## 沉睡候选（带本地模型建议）", ""]
        if a.no_llm:
            for it in sorted(b["sleeping"], key=lambda x: -x.get("age", 0))[:40]:
                lines.append(f"- `{it['file']}:{it['line']}`（{it['age']} 天）{it['text'][:110]}")
        else:
            pairs = summarize_sleeping(sorted(b["sleeping"], key=lambda x: -x.get("age", 0))[:24])
            for it, sug in zip(sorted(b["sleeping"], key=lambda x: -x.get("age", 0))[:24], pairs):
                lines.append(f"- **{it['age']} 天** `{it['file']}:{it['line']}` — {sug}")
                lines.append(f"  - 原文：{it['text'][:150]}")
        lines.append("")

    if b["undated"]:
        c = Counter(it["file"] for it in b["undated"])
        lines += ["## 无日期标记的条目（按文件）", "",
                  "> V1.1 规则要求每条带「· 最后提及 YYYY-MM-DD」；这些没有，遗忘机制扫不到。", ""]
        lines += [f"- {f}：{n} 条" for f, n in c.most_common()]
        lines.append("")

    # ---- 文件级沉睡（兜底判据：mtime）----
    today_d = date.today()
    fstats = []
    for f in sorted(MEM.glob("*.md")):
        st = f.stat()
        age = (today_d - date.fromtimestamp(st.st_mtime)).days
        n_mark = len(MARK.findall(f.read_text(encoding="utf-8", errors="replace")))
        fstats.append((f.name, age, n_mark, len(f.read_text(encoding="utf-8", errors="replace").splitlines())))
    lines += ["## 文件级沉睡（条目日期没落地时的兜底判据）", "",
              "> ⚠️ 本轮发现：**V1.1 的「· 最后提及」几乎没写进文件**，条目级扫不出沉睡 → 改用文件 mtime 兜底。", "",
              "| 文件 | 行数 | 天数没动 | 带日期标记 |", "|---|---:|---:|---:|"]
    for name, age, n_mark, nline in sorted(fstats, key=lambda x: -x[1]):
        lines.append(f"| `{name}` | {nline} | **{age}** | {n_mark} |")
    lines.append("")

    # ---- 双判据沉睡：mtime 与「检索命中」都超过阈值 → 归档候选 ----
    import json as _json
    last_hit: dict[str, int] = {}
    if HITLOG.exists():
        for raw in HITLOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = _json.loads(raw)
            except Exception:
                continue
            fn = rec.get("file")
            ts = int(rec.get("ts") or 0)
            if fn and ts > last_hit.get(fn, 0):
                last_hit[fn] = ts
    lines += ["## 双判据沉睡（B 方案：mtime + 检索命中）", "",
              f"> 判据：**{a.days} 天**既没被改写，也没被检索命中 → 归档候选。", ""]
    if not last_hit:
        lines += ["> ⚠️ 引用日志还是空的（刚上线）。先积累几天，下次体检才判得准。", ""]
    lines += ["| 文件 | 天数没改写 | 最后被检索命中 | 判定 |", "|---|---:|---|---|"]
    now_ts = int(datetime.now().timestamp())
    for name, age, n_mark, nline in sorted(fstats, key=lambda x: -x[1]):
        hit = last_hit.get(name)
        hit_txt = datetime.fromtimestamp(hit).strftime("%m-%d %H:%M") if hit else "从未"
        hit_age = (now_ts - hit) / 86400 if hit else 999
        verdict = "**归档候选**" if age >= a.days and hit_age >= a.days else "保留"
        lines.append(f"| `{name}` | {age} | {hit_txt}（{hit_age:.0f} 天前）| {verdict} |")
    lines.append("")

    lines += ["## 下一步（人工/我确认后执行）", "",
              "1. 沉睡条目 **原文搬进 `90-遗忘.md`**（带归档日期+原因），索引删行",
              "2. 判为「合并」的条目并进同主题文件",
              "3. 给无日期条目补 `· 最后提及`",
              "4. 打一份架构快照（改记忆体必做）", ""]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / f"记忆体检-{today}.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    print(f"  常青 {len(b['evergreen'])} · 沉睡 {len(b['sleeping'])} · 新鲜 {len(b['fresh'])} · 无日期 {len(b['undated'])}")
    print(f"  报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
