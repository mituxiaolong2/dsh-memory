#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_digest.py —— 会话区 v0（记忆体 V2.0 的第一块，2026-11-14）

作用：把「最近三天聊了什么」压成 5–8 行，写进 `61-近三天.md`（滚动窗口）。
为什么要有：换会话后我只知道记忆文件里的事，不知道"这几天跟你聊了什么"。

零 API 成本：读会话尾帧（zstd 多帧，只解尾部）→ 本地 Bonsai 摘要。

用法：
  python session_digest.py            # 生成今天的摘要（覆盖当天那段）
  python session_digest.py --days 3   # 生成时顺带滚掉 3 天前的
  python session_digest.py --dry      # 只看抽出来的原文，不写文件
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
SESS_ROOT = HOME / ".dsh" / "sessions"
DIGEST = HOME / ".dsh" / "memory" / "61-近三天.md"
LAZY_HOME = HOME / ".dsh" / "cache"
ENGINE = "http://127.0.0.1:8095/v1/chat/completions"
MODEL = "bonsai27b"
MAGIC = b"\x28\xb5\x2f\xfd"
MAX_FRAMES = 600          # 从尾部最多解这么多帧（1 帧≈1 次写入）
MAX_CHARS = 6000          # 送给本地模型的对话原文上限


def newest_session_file() -> Path | None:
    best, mtime = None, 0.0
    for p in SESS_ROOT.rglob("session.v4.jsonl.zstd"):
        t = p.stat().st_mtime
        if t > mtime:
            best, mtime = p, t
    return best


def frame_starts(data: bytes) -> list[int]:
    pos, out = 0, []
    while True:
        i = data.find(MAGIC, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 4
    return out


def read_tail_lines(frames: int = 400) -> list[str]:
    """从尾部 frames 帧开始解压一次（后缀是后续所有帧的拼接）。"""
    import compression.zstd as zstd  # Python 3.14 标准库
    p = newest_session_file()
    if p is None:
        return []
    data = p.read_bytes()
    starts = frame_starts(data)
    if not starts:
        return []
    start = starts[-frames] if len(starts) >= frames else starts[0]
    try:
        raw = zstd.decompress(data[start:]).decode("utf-8", "replace")
    except Exception:
        return []
    return raw.splitlines()


def extract_turns(lines: list[str]) -> list[tuple[str, str]]:
    """user/message → data.content[]；assistant/message → data.message.content[]。按 seq 去重。"""
    seen: set = set()
    turns: list[tuple[str, str]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type") or ""
        if kind not in ("user/message", "assistant/message"):
            continue
        seq = rec.get("seq")
        if seq in seen:
            continue
        seen.add(seq)
        data = rec.get("data") or {}
        if kind == "user/message":
            parts = data.get("content") or []
            role = "user"
        else:
            parts = (data.get("message") or {}).get("content") or []
            role = "assistant"
        texts = [p.get("text", "") for p in parts
                 if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        text = " ".join(texts).strip()
        if not text:
            continue
        if text.startswith("[看屏幕]") or text.startswith("[SCHEDULE") or text.startswith("[渠道"):
            continue
        turns.append((role, text))
    return turns


def local_summary(turns: list[tuple[str, str]]) -> str:
    convo = "\n".join(f"{'我' if r=='user' else '助手'}：{t[:300]}" for r, t in turns)[-MAX_CHARS:]
    prompt = (
        "下面是今天我和助手的对话片段。请压成 5–8 行中文摘要，每行格式：\n"
        "- 主题一句话 ｜ 关键词: 词1, 词2\n"
        "只写有信息量的（决定、任务、偏好、约定、情绪状态），不要寒暄，不要复述代码。\n\n"
        + convo
    )
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 400, "temperature": 0.3}).encode("utf-8")
    req = urllib.request.Request(ENGINE, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()


def write_digest(lines: list[str], keep_days: int) -> Path:
    today = date.today()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    header = [
        "# 近三天 · 会话摘要（记忆体 V2.0 会话区）",
        "",
        "> 主题：最近 3 天我跟用户聊了什么（滚动窗口，超期自动滚进 `60-生活.md`）",
        "> 上限：**120 行**｜建立原因：V2 蓝图里的「实时记忆·会话区」，2026-11-14 自主施工",
        "> 生成：`session_digest.py`（读会话尾帧 + 本地 Bonsai 摘要，零 API 成本）",
        "",
    ]
    old_blocks: list[str] = []
    if DIGEST.exists():
        cur, buf = None, []
        for ln in DIGEST.read_text(encoding="utf-8").splitlines():
            if ln.startswith("## "):
                if cur and buf:
                    old_blocks.append("\n".join(buf).rstrip())
                cur, buf = ln, [ln]
            elif cur:
                buf.append(ln)
        if cur and buf:
            old_blocks.append("\n".join(buf).rstrip())

    # 丢掉超过 keep_days 的旧段
    kept = [b for b in old_blocks if b[:12] > f"## {cutoff}"[:12] or not b.startswith("## ")]
    today_block = f"## {today.isoformat()}\n\n" + "\n".join(lines)
    body = "\n\n".join(kept + [today_block])
    DIGEST.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8")
    return DIGEST


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    lines = read_tail_lines()
    turns = extract_turns(lines)
    print(f"  会话尾帧里抽出今天的消息：{len(turns)} 条（user {sum(1 for r,_ in turns if r=='user')}）")
    if not turns:
        print("  没有今天的消息，跳过")
        return 0
    if a.dry:
        for r, t in turns[-12:]:
            print(f"    {'我' if r=='user' else '助手'}：{t[:80]}")
        return 0

    try:
        summary = local_summary(turns)
        print("  ✅ 本地模型摘要完成")
    except Exception as e:
        summary = "\n".join(f"- （本地模型不可用，原文片段）{t[:60]}" for _, t in turns[-6:])
        print(f"  ⚠️ 本地模型失败，退回原文：{e}")
    out = write_digest([summary], a.days)
    print(f"  已写入 {out}（{len(summary.splitlines())} 行摘要）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
