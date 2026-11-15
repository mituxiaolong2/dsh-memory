#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
three_engines.py —— 三自引擎 v1 调度器（2026-11-14）

自抉择（decide）→ 执行（act）→ 记录（record）→ 自学习（learn）→ 自遗忘（forget，按需）

设计原则：
  · **可核对**：每件事都必须产出可验证的东西（报告/文件/校验结果），否则算失败
  · **不自嗨**：失败 3 次连续 → 自动降权并冷却 24 小时；权重区间 [0.3, 1.5]
  · **零 API 成本**：全部本地脚本 + 本地小模型，不花余额
  · **遗忘只在 --forget-due 或每月 1 号真正执行**，且执行前必打快照

用法：
  python three_engines.py --cycle          # 跑一轮：学→选→做→记
  python three_engines.py --cycle --forget-due   # 顺带执行遗忘（会搬家，先快照）
  python three_engines.py --status         # 看权重与历史
"""
from __future__ import annotations

import argparse
import os
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".dsh" / "cache"
WEIGHTS = CACHE / "self-learn.json"
HISTORY = CACHE / "self-direct.jsonl"
LIFE = HOME / ".dsh" / "memory" / "60-生活.md"
LAUNCH = Path(os.path.expandvars(r"$DSH_TOOLS\launcher"))
PY = sys.executable or "python"

POOL = [
    {"id": "backup_verify", "name": "校验记忆备份一致性", "urgency": 0.7,  "value": 0.85, "cost": 0.1,  "cooldown": 72},
    {"id": "disk_health",   "name": "磁盘/余额/内存体检", "urgency": 0.6,  "value": 0.6,  "cost": 0.1,  "cooldown": 12},
    {"id": "memory_audit",  "name": "记忆体检报告",       "urgency": 0.5,  "value": 0.8,  "cost": 0.3,  "cooldown": 168},
    {"id": "doc_index",     "name": "产出文档索引",       "urgency": 0.25, "value": 0.45, "cost": 0.2,  "cooldown": 168},
    {"id": "note_digest",   "name": "翻云笔记摘一句",     "urgency": 0.3,  "value": 0.5,  "cost": 0.3,  "cooldown": 24},
    {"id": "forget",        "name": "自遗忘搬家（打快照）", "urgency": 0.15, "value": 0.6,  "cost": 0.4,  "cooldown": 720},
    {"id": "idle",          "name": "什么都不做",         "urgency": 0.05, "value": 0.2,  "cost": 0.0,  "cooldown": 0},
]


def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_weights() -> dict:
    return load_json(WEIGHTS, {"tasks": {}})


def weight_of(w: dict, tid: str) -> float:
    t = w["tasks"].get(tid)
    if not t:
        return 1.0
    return max(0.3, min(1.5, float(t.get("weight", 1.0))))


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    out = []
    for raw in HISTORY.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


# ---------------- 任务实现：每个都必须可核对 ----------------

def t_backup_verify() -> tuple[bool, str]:
    rc, out = run([PY, "-X", "utf8", str(LAUNCH / "dsh-memory-backup.py"), "--verify"])
    ok = rc == 0 and ("✓" in out or "完全一致" in out)
    if not ok:  # 不一致 → 立刻重做一份
        run([PY, "-X", "utf8", str(LAUNCH / "dsh-memory-backup.py"), "--no-cloud"])
        rc2, out2 = run([PY, "-X", "utf8", str(LAUNCH / "dsh-memory-backup.py"), "--verify"])
        ok = rc2 == 0 and ("✓" in out2 or "完全一致" in out2)
        return ok, ("发现不一致→已重建备份后一致" if ok else "发现不一致且重建后仍不一致")
    return True, "校验一致"


def t_disk_health() -> tuple[bool, str]:
    import shutil
    free_c = shutil.disk_usage("C:/").free / 1e9
    free_e = shutil.disk_usage("E:/").free / 1e9
    usage = load_json(HOME / ".dsh" / ".dshw-usage.json", {})
    bal = usage.get("lastBalance")
    ok = free_c > 5 and (bal is None or bal > 10)
    return ok, f"C剩{free_c:.1f}G E剩{free_e:.1f}G 余额{bal}元"


def t_memory_audit() -> tuple[bool, str]:
    rc, out = run([PY, "-X", "utf8", str(LAUNCH / "memory_audit.py"), "--no-llm"])
    report = Path(os.path.expandvars(r"$DSH_WORK\产出文档")) / f"记忆体检-{date.today()}.md"
    ok = rc == 0 and report.exists()
    return ok, (f"报告已生成 {report.name}" if ok else "报告没生成")


def t_doc_index() -> tuple[bool, str]:
    root = Path(os.path.expandvars(r"$DSH_WORK\产出文档"))
    rows = [p for p in root.rglob("*") if p.is_file() and p.name not in ("索引.md", "index.md")]
    lines = ["# 产出文档索引", "", f"> 自动生成 {datetime.now():%Y-%m-%d %H:%M} · {len(rows)} 个文件", ""]
    for p in sorted(rows):
        lines.append(f"- `{p.relative_to(root).as_posix()}`（{p.stat().st_size/1024:.0f} KB）")
    (root / "索引.md").write_text("\n".join(lines), encoding="utf-8")
    return True, f"索引 {len(rows)} 个文件"


def _local_summary(text: str, prompt_prefix: str, max_tokens: int = 160) -> str | None:
    """调本地 Bonsai（零 API 成本）做摘要；失败返回 None。"""
    import json as _json
    import urllib.request
    body = _json.dumps({
        "model": "bonsai27b",
        "messages": [{"role": "user", "content": prompt_prefix + text[:1800]}],
        "max_tokens": max_tokens, "temperature": 0.3,
    }).encode("utf-8")
    try:
        req = urllib.request.Request("http://127.0.0.1:8095/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = _json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
        return out if out else None
    except Exception:
        return None


def t_note_digest() -> tuple[bool, str]:
    rc, out = run(["youdaonote", "list"], timeout=120)
    if rc != 0 or not out:
        return False, "云笔记 CLI 不可用"
    notes = [ln for ln in out.splitlines() if ".note" in ln or ".md" in ln]
    if not notes:
        return False, "没有可读笔记"
    line = notes[int(time.time()) % len(notes)]
    nid = line.split("\t")[0].strip().split()[-1] if "\t" in line else ""
    body_text = ""
    if nid:
        rc2, out2 = run(["youdaonote", "read", nid], timeout=120)
        if rc2 == 0 and out2:
            body_text = out2
    if not body_text.strip():
        return False, "读不到笔记正文"

    # 2026-11-15 升级：本地模型做真摘要（以前只截前 6 行）
    summary = _local_summary(
        body_text,
        "下面是我自己云笔记的一段内容。用一句中文（不超过 40 字）概括它讲了什么，"
        "再加一句「我会记住的点：…」（不超过 30 字）。只输出这两行，不要解释。\n\n",
    )
    if summary:
        line_out = " ".join(summary.splitlines())[:180]
        how = "本地模型摘要"
    else:
        line_out = " ".join(body_text.split(chr(10))[:6])[:140]
        how = "截取原文（本地模型不可用）"

    LIFE.parent.mkdir(parents=True, exist_ok=True)
    with LIFE.open("a", encoding="utf-8") as fh:
        fh.write(f"\n- {datetime.now():%H:%M} 【自主·翻笔记·{how}】{line_out} · {date.today()}\n")
    return True, how


def t_forget() -> tuple[bool, str]:
    rc, out = run([PY, "-X", "utf8", str(LAUNCH / "engine_forget.py"), "--apply", "--limit", "10"], timeout=180)
    moved = "搬走" in out
    return rc == 0 and moved, ("已搬走候选块" if moved else "没有候选，未动")


DISPATCH = {
    "backup_verify": t_backup_verify,
    "disk_health": t_disk_health,
    "memory_audit": t_memory_audit,
    "doc_index": t_doc_index,
    "note_digest": t_note_digest,
    "forget": t_forget,
}


# ---------------- 学习：按结果调权重 ----------------

def learn(hist: list[dict], w: dict) -> list[str]:
    notes = []
    for item in POOL:
        tid = item["id"]
        if tid == "idle" or tid not in DISPATCH:
            continue
        runs = [h for h in hist if h.get("picked") == tid and "ok" in h][-5:]
        if not runs:
            continue
        succ = sum(1 for h in runs if h["ok"])
        node = w["tasks"].setdefault(tid, {"weight": 1.0, "runs": 0, "fails": 0, "last": 0})
        node["runs"] = node.get("runs", 0) + 0
        old = node["weight"]
        if succ == len(runs):
            node["weight"] = min(1.5, old + 0.05)
        elif succ == 0 and len(runs) >= 2:
            node["weight"] = max(0.3, old - 0.1)
            node["fails"] = node.get("fails", 0) + 1
            if node["fails"] >= 3:
                node["last"] = int(time.time())
                notes.append(f"{tid} 连续失败→降权+冷却")
        if abs(node["weight"] - old) > 1e-9:
            notes.append(f"{tid} 权重 {old:.2f}→{node['weight']:.2f}")
    return notes


BALANCE_GUARD = 10.0   # 余额低于这个数，只允许体检类任务（遵从用户的余额纪律）


def balance() -> float | None:
    u = load_json(HOME / ".dsh" / ".dshw-usage.json", {})
    b = u.get("lastBalance")
    return float(b) if isinstance(b, (int, float)) else None


def review(w: dict, hist: list[dict]) -> list[dict]:
    """候选池体检：谁该休眠、谁该复活（2026-11-15 加，针对"三自引擎自抉择"的淘汰机制）。

    规则：
      · 被选过 ≥3 次且一次都没成功 → 休眠 7 天（dis 字段存到期时间戳）
      · 休眠中且超过 7 天没再被验证 → 复活一次（给一次机会）
      · 30 天内一次没被选中且成本 > 0.25 → 标 dormant（只降权，不删）
    """
    now = time.time()
    out = []
    for item in POOL:
        tid = item["id"]
        if tid == "idle" or tid not in DISPATCH:
            continue
        node = w["tasks"].setdefault(tid, {"weight": 1.0, "runs": 0, "fails": 0, "last": 0})
        runs = [h for h in hist if h.get("picked") == tid and "ok" in h]
        succ = sum(1 for h in runs if h["ok"])
        chosen = len(runs)

        # ① 一直失败 → 休眠
        if chosen >= 3 and succ == 0 and not node.get("dis"):
            node["dis"] = now + 7 * 86400
            out.append({"id": tid, "action": "休眠 7 天", "reason": f"被选 {chosen} 次全失败"})
        # ② 休眠到期 → 复活一次
        elif node.get("dis") and now > node["dis"]:
            node["dis"] = 0
            node["fails"] = 0
            out.append({"id": tid, "action": "复活（给一次机会）", "reason": "休眠期已满"})
        # ③ 长期没人选 + 成本不低 → 标 dormant
        elif chosen == 0 and item["cost"] > 0.25 and not node.get("dormant"):
            node["dormant"] = True
            out.append({"id": tid, "action": "标 dormant", "reason": "30 天内没被选中过且成本偏高"})
    return out


def decide(w: dict, hist: list[dict], allow_forget: bool) -> tuple[dict, list[dict]]:
    """两轮挑：优先只能选"不在冷却"的；实在没有才放宽（2026-11-15 改）。

    之前的写法是"冷却中分数打 0.4 折"——结果 backup_verify 每 3 小时都赢，
    等于候选池形同虚设。改成硬冷却。
    """
    now = time.time()
    low_balance = (balance() or 999) < BALANCE_GUARD

    def build(strict: bool) -> list[dict]:
        rows = []
        for item in POOL:
            tid = item["id"]
            if tid == "forget" and not allow_forget:
                continue
            if low_balance and tid not in ("disk_health", "backup_verify", "idle"):
                continue
            node = w["tasks"].get(tid, {})
            if node.get("dis") and now < node["dis"]:
                continue
            last_ok = max([h["ts"] for h in hist if h.get("picked") == tid and h.get("ok")] or [0])
            cool = now - last_ok < item["cooldown"] * 3600
            if node.get("fails", 0) >= 3 and now - node.get("last", 0) < 86400:
                cool = True
            if strict and cool:
                continue
            base = 0.5 * item["urgency"] + 0.3 * item["value"] - 0.2 * item["cost"]
            score = base * weight_of(w, tid)
            if node.get("dormant"):
                score *= 0.5      # 长期没选中的：降优先，但不禁止
            rows.append({**item, "score": round(score, 3), "cooling": cool})
        rows.sort(key=lambda x: -x["score"])
        return rows

    rows = build(strict=True)
    if not rows:
        rows = build(strict=False)
        if rows:
            rows[0]["note"] = "所有都在冷却，放宽一次"
    return rows[0], rows


def cycle(allow_forget: bool) -> int:
    w = load_weights()
    hist = load_history()

    learned = learn(hist, w)
    reviewed = review(w, hist)
    save_json(WEIGHTS, w)

    best, rows = decide(w, hist, allow_forget)
    print("  排序：")
    for r in rows:
        print(f"  {r['score']:>6.3f}  {r['id']:<14} {r['name']}{'  [冷却]' if r['cooling'] else ''}")
    print(f"\n  → 选中：{best['id']} · {best['name']}")

    rec = {"ts": int(time.time()), "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "picked": best["id"], "name": best["name"], "score": best["score"],
           "runner_up": [{"id": r["id"], "score": r["score"]} for r in rows[1:3]],
           "learned": learned}

    # 被选中了就说明它还有用 → 摘掉 dormant
    node = w["tasks"].setdefault(best["id"], {"weight": 1.0, "runs": 0, "fails": 0, "last": 0}) if best["id"] in DISPATCH else None
    if node and node.get("dormant"):
        node["dormant"] = False
        save_json(WEIGHTS, w)

    ok, detail = (True, "idle")
    if best["id"] in DISPATCH:
        t0 = time.time()
        try:
            ok, detail = DISPATCH[best["id"]]()
        except Exception as e:
            ok, detail = False, f"异常：{type(e).__name__}: {e}"
        rec["duration_ms"] = int((time.time() - t0) * 1000)
    rec["ok"] = ok
    rec["detail"] = detail

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  执行结果：{'✅' if ok else '❌'} {detail}")
    if learned:
        print("  学到：" + "；".join(learned))
    if reviewed:
        print("  淘汰：" + "；".join(f"{r['id']} {r['action']}（{r['reason']}）" for r in reviewed))

    LIFE.parent.mkdir(parents=True, exist_ok=True)
    with LIFE.open("a", encoding="utf-8") as fh:
        fh.write(f"\n- {datetime.now():%H:%M} 【自主·三自引擎】{best['name']} → "
                 f"{'成功' if ok else '失败'}（{detail}）· {date.today()}\n")
    return 0 if ok else 1


def review_cmd() -> int:
    w = load_weights()
    hist = load_history()
    rows = []
    for item in POOL:
        tid = item["id"]
        runs = [h for h in hist if h.get("picked") == tid and "ok" in h]
        if tid == "idle":
            continue
        succ = sum(1 for h in runs if h["ok"])
        node = w["tasks"].get(tid, {})
        rows.append((tid, len(runs), succ, node.get("weight", 1.0),
                     bool(node.get("dis") and node["dis"] > time.time()), node.get("dormant", False)))
    print("  候选池体检：")
    print("    id                被选  成功  权重  休眠  dormant")
    for r in rows:
        print(f"    {r[0]:<16} {r[1]:>4} {r[2]:>5}  {r[3]:.2f}  {'是' if r[4] else '否':<2}   {'是' if r[5] else '否'}")
    return 0


def status() -> int:
    w = load_weights()
    hist = load_history()
    print("  权重：")
    for tid, node in w["tasks"].items():
        print(f"    {tid:<14} weight={node.get('weight'):.2f} fails={node.get('fails',0)}")
    runs = [h for h in hist if "ok" in h][-10:]
    print("  最近 10 次：")
    for h in runs:
        print(f"    {h['at']}  {h['picked']:<14} {'✅' if h['ok'] else '❌'}  {h.get('detail','')[:40]}")
    succ = sum(1 for h in runs if h["ok"])
    print(f"  成功率（近 10）：{succ}/{len(runs) if runs else 0}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", action="store_true")
    ap.add_argument("--forget-due", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--review", action="store_true")
    a = ap.parse_args()
    if a.review:
        sys.exit(review_cmd())
    if a.status:
        sys.exit(status())
    if a.cycle or a.forget_due:
        due = a.forget_due or date.today().day == 1
        sys.exit(cycle(allow_forget=due))
    status()
