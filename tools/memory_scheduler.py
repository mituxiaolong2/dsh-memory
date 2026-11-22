#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""memory_scheduler.py — 记忆体 V2.1 的调度层（谁执行、多久一次）

三个频率，对应三种维护动作。默认全部**只读或只写派生产物**，真正的搬移只在
`--daily` 的「摘要下沉」里发生（那是低风险、可回滚的一行+指针）。

  python -X utf8 memory_scheduler.py --daily     # 每天 23:40：摘要限长 + 第 4 天下沉
  python -X utf8 memory_scheduler.py --weekly    # 每周日 13:00：周检 + 分片检查 + 完成区热度
  python -X utf8 memory_scheduler.py --monthly   # 每月 1 号：常青候选 + 索引核对 + 快照提醒
  python -X utf8 memory_scheduler.py --daily --dry-run
"""
from __future__ import annotations
import os

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

MEM = Path.home() / ".dsh" / "memory"
from _paths import TOOLS_DIR as LAUNCHER  # noqa: E402
REPORT_DIR = Path(os.environ.get("DSH_DL_DIR", Path.home() / ".dsh" / "_dl"))
HITLOG = Path.home() / ".dsh" / "cache" / "memory-hits.jsonl"

DIGEST = MEM / "11-近三天.md"
LIFE = MEM / "31-生活.md"
INDEX = MEM / "00-索引.md"
DONE = MEM / "70-完成.md"
LIMIT_DIGEST = 4000
LIMIT_SHORT = 40_000     # 短期层 / 长期层统一
LIMIT_FORGET = 100_000   # 遗忘区例外（不加载，只为可维护性设上限）
KEEP_DAYS = 3


def sh(cmd) -> str:
    r = subprocess.run([sys.executable, "-X", "utf8", *cmd], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    return ((r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")).strip()


# ---------------------------------------------------------------- 每日
def daily(dry: bool) -> int:
    print("=" * 56)
    print(f"记忆体日维护 {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 56)
    if not DIGEST.exists():
        print("没有 11-近三天.md，跳过")
        return 0
    text = DIGEST.read_text(encoding="utf-8")
    head, *sections = re.split(r"(?m)^## ", text)
    kept, sunk = [], []
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    for sec in sections:
        m = re.match(r"(20\d{2}-\d{2}-\d{2})", sec)
        if not m:
            kept.append(sec)
            continue
        d = date.fromisoformat(m.group(1))
        if d >= cutoff:
            kept.append(sec)
        else:
            first = next((ln.strip(" -") for ln in sec.splitlines()[1:] if ln.strip()), "")
            sunk.append((m.group(1), first[:70]))
    print(f"  保留 {len(kept)} 段 ｜ 下沉 {len(sunk)} 段")
    if sunk and not dry:
        with open(LIFE, "a", encoding="utf-8") as fh:
            fh.write("\n#### 会话摘要下沉（%s）\n" % date.today().isoformat())
            for d, line in sunk:
                fh.write(f"- {d}：{line} · 详见 `41-项目档案`\n")
    new = (head + "".join(f"## {s}" for s in kept)).rstrip("\n") + "\n"
    n = len(new)
    print(f"  摘要长度 {n} 字符（上限 {LIMIT_DIGEST}）" + ("  ⚠ 超了要精简" if n > LIMIT_DIGEST else "  ✓"))
    if n > LIMIT_DIGEST:
        print("  提示：让摘要只保留「决定了什么 / 还没解决什么」两类，细节走 41-项目档案")
    if not dry:
        DIGEST.write_text(new, encoding="utf-8", newline="\n")
        print("  已写回 11-近三天.md")
    return 0


# ---------------------------------------------------------------- 每周
def hits_review() -> str:
    """本周命中 / 回忆日志复盘（V2.1 ⑩）"""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td

    cache = HITLOG.parent
    lines = []
    week_ago = _dt.now() - _td(days=7)

    def _count(path: Path) -> tuple[int, int, dict]:
        total = recent = 0
        per_file: dict[str, int] = {}
        if not path.exists():
            return 0, 0, {}
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                h = _json.loads(ln)
            except Exception:
                continue
            total += 1
            raw = h.get("ts") or h.get("at") or ""
            when = None
            if isinstance(raw, (int, float)):
                when = _dt.fromtimestamp(raw)          # 日志里 ts 是 unix 时间戳
            else:
                s = str(raw)
                try:
                    when = _dt.fromisoformat(s[:19])
                except Exception:
                    try:
                        when = _dt.fromtimestamp(float(s))
                    except Exception:
                        when = None
            if when and when >= week_ago:
                recent += 1
                f = h.get("file") or h.get("src") or "?"
                per_file[f] = per_file.get(f, 0) + 1
        return total, recent, per_file

    hit_total, hit_week, hit_files = _count(HITLOG)
    rec_total, rec_week, rec_files = _count(cache / "memory-recall.jsonl")

    lines.append(f"- 命中日志：累计 {hit_total} 条，本周 {hit_week} 条")
    if hit_files:
        top = sorted(hit_files.items(), key=lambda kv: -kv[1])[:5]

        def _label(name: str) -> str:
            # 改名前的旧记录（磁盘上已没有这个文件）标注出来
            base = str(name).split(":")[0]
            if (MEM / base).exists() or (MEM.parent / base).exists():
                return name
            return f"{name}（旧名）"

        lines.append("  · 本周热门：" + "、".join(f"{_label(k)}×{v}" for k, v in top))
    else:
        lines.append("  · 本周没有命中记录（一周没检索过记忆？）")
    lines.append(f"- 回忆日志：累计 {rec_total} 条，本周 {rec_week} 条")
    if rec_total < 10:
        lines.append("  · ⚠ 回忆触发太少，阈值可能偏严（或长期层内容还太少）")
    elif rec_week == 0:
        lines.append("  · 本周没触发回忆，正常（没问老事）")
    lines.append("- 复盘建议：把「本周热门」里反复命中的条目标 ⭐常青；")
    lines.append("  从没命中又没被正文引用的文件，考虑进归档。")
    return "\n".join(lines)


def weekly() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"记忆周检-{date.today():%Y%m%d}.md"
    parts = [f"# 记忆体周检 {datetime.now():%Y-%m-%d %H:%M}", ""]

    print("→ 检索账本 …", flush=True)
    parts += ["## 检索账本", "", "```", sh([LAUNCHER / "retrieval_report.py"]), "```", ""]
    print("→ 漏写检测 …", flush=True)
    parts += ["## 漏写检测", "", "```", sh([LAUNCHER / "memory_gap_check.py", "--days", "7"]), "```", ""]
    print("→ 遗忘体检 …", flush=True)
    parts += ["## 遗忘体检", "", "```", sh([LAUNCHER / "forget_plan.py"]), "```", ""]

    print("→ 元数据旁挂重建 …", flush=True)
    parts += ["## 元数据旁挂 (.meta.jsonl)", "", "```", sh([LAUNCHER / "meta_build.py"]), "```", ""]

    # 分片检查
    print("→ 分片检查 …", flush=True)
    lines = ["## 分片检查（短期 40,000 / 长期 40,000 / 遗忘区 100,000 字符）", ""]
    for p in sorted(MEM.glob("*.md")):
        chars = len(p.read_text(encoding="utf-8"))
        if p.name[:2] in ("90", "91", "92"):
            limit = LIMIT_FORGET          # 遗忘区例外
        elif p.name == "00-索引.md":
            limit = 6000
        else:
            limit = LIMIT_SHORT           # 短期层与长期层统一 40,000
        if chars > limit:
            lines.append(f"- ⚠ `{p.name}` {chars:,} 字符 > {limit:,} → 该分片了")
    if len(lines) == 2:
        lines.append("- ✓ 没有文件超限")
    parts += lines + [""]

    # 完成区热度
    print("→ 完成区热度 …", flush=True)
    parts += ["## 完成区热度（30 天没被命中 → 应进 71-归档）", ""]
    if DONE.exists():
        hits = set()
        if HITLOG.exists():
            cut = time.time() - 30 * 86400
            for ln in HITLOG.read_text(encoding="utf-8").splitlines():
                try:
                    h = json.loads(ln)
                except Exception:
                    continue
                if h.get("ts", 0) >= cut:
                    hits.add(h.get("file"))
        parts.append("- 近 30 天被命中过：" + ("、".join(sorted(h for h in hits if h)) or "无"))
        parts.append(f"- 判定：`70-完成.md` {'最近有命中，先留着' if '70-完成.md' in hits else '30 天没命中 → 建议把已收尾条目搬 71-归档'}")
    else:
        parts.append("- 没有 70-完成.md")
    parts.append("")

    parts += ["## 命中 / 回忆复盘", "", hits_review(), ""]

    out.write_text("\n".join(parts) + "\n", encoding="utf-8", newline="\n")
    print(f"周检完成：{out}")
    return 0


# ---------------------------------------------------------------- 每月
OLD_NAME_MAP = {      # 命中日志里可能出现改名前的旧文件名
    "10-主人.md": "20-主人.md",
    "20-项目与工作区.md": "40-项目.md",
    "30-约定.md": "21-约定.md",
    "30-时间线.md": "30-时间线.md",
    "50-时间线.md": "30-时间线.md",
    "64-人格札记.md": "52-人格札记.md",
    "63-见闻.md": "32-见闻.md",
    "60-生活.md": "31-生活.md",
}


def monthly() -> int:
    lines: list[str] = []
    lines.append(f"# 记忆体月复盘 {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")
    hits = {}
    if HITLOG.exists():
        for ln in HITLOG.read_text(encoding="utf-8").splitlines():
            try:
                h = json.loads(ln)
            except Exception:
                continue
            raw = h.get("file") or ""
            name = OLD_NAME_MAP.get(raw, raw)
            hits[name] = hits.get(name, 0) + 1
    lines.append("【常青候选】被命中 ≥3 次（可标 ⭐常青，永不遗忘）")
    ever = [(f, n) for f, n in sorted(hits.items(), key=lambda kv: -kv[1]) if n >= 3]
    lines += [f"  {f}  命中 {n} 次" for f, n in ever] or ["  无"]
    lines.append("")
    lines.append("【索引核对】00-索引 里点名的文件 vs 磁盘上实际有的")
    if INDEX.exists():
        idx = INDEX.read_text(encoding="utf-8")
        listed = set(re.findall(r"`(\d{2}-[^`]+?\.md)`", idx))
        actual = {x.name for x in MEM.glob("*.md")} - {"00-索引.md"}
        lines.append(f"  磁盘有、索引没写： {sorted(actual - listed) or '无'}")
        lines.append(f"  索引写了、磁盘没有： {sorted(listed - actual) or '无'}")
    lines.append("")
    # V2.1 差距 ④⑥ 的检查：完成区/归档区/遗忘区体量 + 长期层分片
    lines.append("【分区体量】")
    caps = {"70-完成.md": 40000, "71-归档.md": 40000, "90-遗忘.md": 100000}
    for name, cap in caps.items():
        f = MEM / name
        if f.exists():
            n = len(f.read_text(encoding="utf-8"))
            flag = "⚠ 接近上限" if n > cap * 0.9 else ""
            lines.append(f"  {name}: {n} 字符 / 上限 {cap} {flag}")
    big = [(f.name, len(f.read_text(encoding='utf-8')))
           for f in MEM.glob("*.md") if f.name != "90-遗忘.md"]
    over = [x for x in big if x[1] > 40000]
    lines.append(f"  超过 40000 字符、该分片的文件： {over or '无'}")
    lines.append("")
    lines.append("【快照提醒】改过架构就打一份：")
    lines.append(f"  {_paths.WORKSPACE / '鲸鲸' / '架构快照'} 里放 "
                  "记忆架构-v<N>-<日期>.md + memory-v<N>-<日期>.zip")

    text = "\n".join(lines) + "\n"
    print(text)
    out = REPORT_DIR / f"记忆月复盘-{date.today():%Y%m%d}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"报告：{out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="记忆体 V2.1 调度层")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--monthly", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.daily:
        return daily(a.dry_run)
    if a.weekly:
        return weekly()
    if a.monthly:
        return monthly()
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
