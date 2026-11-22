#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine_forget.py —— 自遗忘引擎 v1（2026-11-14 自主施工）

和 v0 的区别：**v0 只出报告，v1 会真的搬**，但搬之前一定打快照。

判据（块级，比文件级准）：
  记忆文件按空行切块，每块有 (文件, 起始行, 结束行)。
  一个块「活着」= 最近 N 天里检索日志命中过落在它行号区间内的记录，
                或者它属于 ⭐常青 / 人格 / 红线 / 索引。
  否则 → 归档候选。

动作（--apply）：
  1. 先把整个 memory/ + AGENTS.md 打快照到 架构快照/memory-<日期>-forget.zip
  2. 候选块原文追加进 90-遗忘.md（带归档日期 + 原因）
  3. 从源文件删除该块
  4. 写一份搬运清单到 ~/.dsh/cache/forget-log.jsonl

用法：
  python engine_forget.py --dry           # 只看候选（默认）
  python engine_forget.py --apply --limit 20
  python engine_forget.py --days 30
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

HOME = Path.home()
MEM = HOME / ".dsh" / "memory"
AGENTS = HOME / ".dsh" / "AGENTS.md"
HITLOG = HOME / ".dsh" / "cache" / "memory-hits.jsonl"
FORGET = MEM / "90-遗忘.md"
from _paths import WORKSPACE  # noqa: E402
SNAP = WORKSPACE / "鲸鲸" / "架构快照"
FLOG = HOME / ".dsh" / "cache" / "forget-log.jsonl"

# 永不遗忘的文件 / 关键词
PROTECTED_FILES = {"00-索引.md", "90-遗忘.md", "20-主人.md", "21-约定.md", "AGENTS.md"}
PROTECTED_WORDS = ("⭐常青", "红线", "余额纪律", "人格", "授权", "别再", "永不")


def load_hits() -> dict[str, list[tuple[int, int]]]:
    """{文件名: [(ts, line), ...]}"""
    hits: dict[str, list[tuple[int, int]]] = {}
    if not HITLOG.exists():
        return hits
    for raw in HITLOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        fn = rec.get("file")
        line = rec.get("line")
        ts = int(rec.get("ts") or 0)
        if fn and isinstance(line, int):
            hits.setdefault(fn, []).append((ts, line))
    return hits


def blocks_of(path: Path) -> list[dict]:
    """按空行切块，保留行号区间。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, buf, start = [], [], None
    for i, ln in enumerate(lines, 1):
        if ln.strip() == "":
            if buf:
                out.append({"start": start, "end": i - 1, "text": "\n".join(buf)})
                buf, start = [], None
            continue
        if start is None:
            start = i
        buf.append(ln)
    if buf:
        out.append({"start": start, "end": len(lines), "text": "\n".join(buf)})
    return out


DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


def newest_date_in(text: str):
    """块里出现的最新日期；没有就 None。"""
    best = None
    for y, m, d in DATE_RE.findall(text):
        try:
            cur = date(int(y), int(m), int(d))
        except ValueError:
            continue
        if best is None or cur > best:
            best = cur
    return best


def is_protected(fname: str, text: str) -> bool:
    if fname in PROTECTED_FILES:
        return True
    return any(w in text for w in PROTECTED_WORDS)


def candidates(days: int) -> list[dict]:
    hits = load_hits()
    cutoff = time.time() - days * 86400
    out = []
    for f in sorted(MEM.glob("*.md")):
        if f.name in PROTECTED_FILES:
            continue
        fh = hits.get(f.name, [])
        for b in blocks_of(f):
            if is_protected(f.name, b["text"]):
                continue
            alive = any(cutoff <= ts and b["start"] <= line <= b["end"] for ts, line in fh)
            if alive:
                continue
            # 守卫①：块内容里写了近期日期 → 不算沉睡（今天刚写的东西不能归档）
            nd = newest_date_in(b["text"])
            if nd is not None and (date.today() - nd).days < days:
                continue
            # 守卫②：文件本身最近改过、块里又没有任何日期 → 保守不归档
            if nd is None and (date.today() - date.fromtimestamp(f.stat().st_mtime)).days < days:
                continue
            out.append({"file": f.name, "start": b["start"], "end": b["end"],
                        "text": b["text"], "chars": len(b["text"]),
                        "text": b["text"]})
    out.sort(key=lambda x: -x["chars"])
    return out


def snapshot() -> Path:
    SNAP.mkdir(parents=True, exist_ok=True)
    p = SNAP / f"memory-{date.today():%Y%m%d}-forget.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for f in MEM.glob("*"):
            if f.is_file():
                z.write(f, f"memory/{f.name}")
        if AGENTS.exists():
            z.write(AGENTS, "AGENTS.md")
    return p


def apply(cands: list[dict], limit: int) -> int:
    if not cands:
        print("  没有候选，什么都不用搬")
        return 0
    picked = cands[:limit]
    snap = snapshot()
    print(f"  ✅ 快照：{snap}（{snap.stat().st_size/1024:.0f} KB）")

    FORGET.parent.mkdir(parents=True, exist_ok=True)
    if not FORGET.exists():
        FORGET.write_text("# 遗忘区\n\n> 已失效/已归档的原文。开局不读。\n", encoding="utf-8")
    today = date.today().isoformat()
    with FORGET.open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n## 归档批次 {today}（自遗忘引擎 v1）\n")
        for c in picked:
            fh.write(f"\n### 来自 `{c['file']}` 原第 {c['start']}–{c['end']} 行\n"
                     f"> 归档日期 {today} · 原因：{c.get('_days', 30)}+ 天未被检索命中且非保护条目\n\n"
                     f"{c['text']}\n")

    # 从源文件删除（按块精确删，倒序删避免行号漂移）
    by_file: dict[str, list[dict]] = {}
    for c in picked:
        by_file.setdefault(c["file"], []).append(c)
    removed = 0
    for fname, items in by_file.items():
        path = MEM / fname
        lines = path.read_text(encoding="utf-8").splitlines()
        for c in sorted(items, key=lambda x: -x["start"]):
            del lines[c["start"] - 1:c["end"]]
            # 顺手吃掉多余空行
            while c["start"] - 1 < len(lines) and lines[c["start"] - 1].strip() == "":
                del lines[c["start"] - 1]
            removed += 1
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    FLOG.parent.mkdir(parents=True, exist_ok=True)
    with FLOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": int(time.time()), "at": datetime.now().isoformat(timespec="seconds"),
                             "snapshot": str(snap), "moved": removed,
                             "files": {k: len(v) for k, v in by_file.items()}}, ensure_ascii=False) + "\n")
    print(f"  ✅ 搬走 {removed} 块 → {FORGET}")
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cands = candidates(a.days)
    total_chars = sum(c["chars"] for c in cands)
    print(f"  候选块：{len(cands)} 个 / 约 {total_chars} 字（判据：{a.days} 天没被检索命中且非保护）")
    for c in cands[:10]:
        print(f"    {c['file']}:{c['start']}-{c['end']}  {c['chars']} 字  {c['text'][:50].replace(chr(10),' ')}")
    if a.apply:
        apply(cands, a.limit)
    else:
        print("  （--dry 模式，没动文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
