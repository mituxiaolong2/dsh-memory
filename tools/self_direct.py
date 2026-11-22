#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self_direct.py —— 自抉择引擎 v0（2026-11-14）

干什么：给"空下来该做什么"这件事一个可比较的算法，而不是每次凭感觉。
  · 候选池固定几类（不自由发挥，防止我给自己发明活干）
  · 打分 = 紧急度×0.5 + 价值×0.3 - 成本×0.2
  · 选最高分，把「为什么选它」写进日志
  · **v0 只推荐不执行**：执行前给人看一眼（或我自己确认），避免自嗨

用法：
  python self_direct.py                 # 推荐一个
  python self_direct.py --top 3         # 看排序
  python self_direct.py --done <id>     # 标记某项已完成（更新冷却）
  python self_direct.py --list          # 看候选池
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".dsh" / "cache" / "self-direct.json"
LOG = HOME / ".dsh" / "cache" / "self-direct.jsonl"
LIFE = HOME / ".dsh" / "memory" / "31-生活.md"

# 候选池：id, 名称, 紧急度, 价值, 成本, 冷却小时, 触发条件说明
POOL = [
    {"id": "memory_audit",    "name": "记忆体检（沉睡候选 + 无日期统计）", "urgency": 0.55, "value": 0.9,  "cost": 0.3, "cooldown": 24 * 7},
    {"id": "backup_verify",   "name": "校验记忆备份逐字节一致",           "urgency": 0.7,  "value": 0.85, "cost": 0.1, "cooldown": 24 * 3},
    {"id": "disk_health",     "name": "磁盘与余额体检",                   "urgency": 0.6,  "value": 0.6,  "cost": 0.1, "cooldown": 12},
    {"id": "meme_tags",       "name": "表情包关键词补全（本地模型批量）", "urgency": 0.2,  "value": 0.55, "cost": 0.35, "cooldown": 24 * 7},
    {"id": "note_digest",     "name": "翻云笔记并摘一句进经历层",         "urgency": 0.3,  "value": 0.5,  "cost": 0.3, "cooldown": 24},
    {"id": "doc_index",       "name": "给产出文档建目录索引",             "urgency": 0.25, "value": 0.45, "cost": 0.25, "cooldown": 24 * 14},
    {"id": "idle",            "name": "什么都不做（省算力）",             "urgency": 0.1,  "value": 0.3,  "cost": 0.0, "cooldown": 0},
]


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"done": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def score(item: dict, st: dict) -> tuple[float, bool]:
    """返回 (分数, 是否在冷却中)"""
    last = st["done"].get(item["id"], 0)
    hours = (time.time() - last) / 3600
    cooling = hours < item["cooldown"]
    base = 0.5 * item["urgency"] + 0.3 * item["value"] - 0.2 * item["cost"]
    if cooling:
        base -= 0.6  # 冷却期内大幅降权，但不完全排除
    return round(base, 3), cooling


def rank(st: dict) -> list[dict]:
    rows = []
    for it in POOL:
        s, cooling = score(it, st)
        rows.append({**it, "score": s, "cooling": cooling})
    rows.sort(key=lambda x: -x["score"])
    return rows


def pick(a) -> int:
    st = load_state()
    rows = rank(st)

    if a.list:
        print("  候选池：")
        for r in rows:
            flag = "冷却中" if r["cooling"] else "可做"
            print(f"  {r['score']:>6.3f}  {r['id']:<14} {r['name']:<26} [{flag}]")
        return 0

    if a.done:
        st["done"][a.done] = int(time.time())
        save_state(st)
        print(f"  ✅ 已标记完成：{a.done}")
        return 0

    top = rows[: max(1, a.top)]
    print("  排序（分数 / 是否冷却）：")
    for r in top:
        print(f"  {r['score']:>6.3f}  {r['id']:<14} {r['name']}{'  [冷却中]' if r['cooling'] else ''}")

    best = top[0]
    if best["id"] == "idle":
        print("\n  → 结论：**什么都不做**（其他项都在冷却，或价值不够）")
        return 0
    print(f"\n  → 推荐执行：**{best['id']}** · {best['name']}（分数 {best['score']}）")

    # 记录抉择：这是「自抉择」的可追溯凭证
    rec = {
        "ts": int(time.time()),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "picked": best["id"],
        "name": best["name"],
        "score": best["score"],
        "runner_up": [{"id": r["id"], "score": r["score"]} for r in top[1:]],
        "reason": f"紧急度{best['urgency']}×0.5 + 价值{best['value']}×0.3 - 成本{best['cost']}×0.2",
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  抉择已记录 → {LOG}")

    if a.log_life:
        LIFE.parent.mkdir(parents=True, exist_ok=True)
        with LIFE.open("a", encoding="utf-8") as fh:
            fh.write(f"\n- {datetime.now():%H:%M} 【自主】自抉择引擎选了「{best['name']}」（分数 {best['score']}，"
                     f"次优 {top[1]['id'] if len(top) > 1 else '-'}）· 2026-11-14\n")
        print("  已写进经历层")
    return 0


if __name__ == "__main__":
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--done", default="")
    ap.add_argument("--log-life", action="store_true")
    sys.exit(pick(ap.parse_args()))
