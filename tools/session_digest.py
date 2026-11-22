#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_digest.py —— 会话区 v0（记忆体 V2.0 的第一块，2026-11-14）

作用：把「最近三天聊了什么」压成 5–8 行，写进 `11-近三天.md`（滚动窗口）。
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

# ---- 2026-11-17 按需拉起本地模型（游戏时延后，空闲自动停）----
def _ensure_model() -> None:
    """确保本地模型可用；显存被游戏占用时直接抛错跳过，不要死等。"""
    try:
        import sys as _sys
        _sys.path.insert(0, r"E:\DSH_work\5.插件\dsh-jingjinglive-鲸鲸生活\记忆体\tools")
        import model_gate  # noqa: WPS433
        rc = model_gate.ensure()
        if rc == 3:
            raise RuntimeError("显存被占用（不一定是游戏，壁纸/浏览器也会占），本地模型本次跳过")
        model_gate.touch()
    except RuntimeError:
        raise
    except Exception:
        pass  # gate 本身坏了不该拖垮主流程



HOME = Path.home()
SESS_ROOT = HOME / ".dsh" / "sessions"
DIGEST = HOME / ".dsh" / "memory" / "11-近三天.md"
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
    convo = "\n".join(f"{'我' if r=='user' else '鲸鲸'}：{t[:300]}" for r, t in turns)[-MAX_CHARS:]
    prompt = (
        "下面是今天我和助手的对话片段。请压成 5–8 行中文摘要，每行格式：\n"
        "- 主题一句话 ｜ 关键词: 词1, 词2\n"
        "只写有信息量的（决定、任务、偏好、约定、情绪状态），不要寒暄，不要复述代码。\n\n"
        + convo
    )
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 400, "temperature": 0.3}).encode("utf-8")
    _ensure_model()
    req = urllib.request.Request(ENGINE, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()


def extractive_summary(turns) -> str:
    """本地模型不可用时的兜底：从会话里抽要点（明确标注是规则摘要）。

    2026-11-21 重写：过滤 harness 噪声（Time sampled / [渠道：微信] / 提醒 JSON）、
    去掉 markdown 表格与代码块，只留第一句，避免把整段塞进实时层。
    """
    import re as _re
    NOISE = (
        "Time sampled while preparing",
        "[渠道：微信]",
        "[图片]",
        "[文件]",
        "reminders_json",
        "You are an AI agent powered",
    )
    bullets = []
    for role, text in reversed(turns):
        if len(bullets) >= 6:
            break
        s = (text or "").strip()
        if not s or any(n in s for n in NOISE):
            continue
        # 去掉代码块 / 表格行 / 多余空行
        s = _re.sub(r"```.*?```", " ", s, flags=_re.S)
        s = _re.sub(r"^\s*\|.*$", " ", s, flags=_re.M)
        s = _re.sub(r"#+\s*", "", s)
        s = _re.sub(r"[*`>]", "", s)
        s = _re.sub(r"\s+", " ", s).strip()
        if not s or s in ("1", "在", "好", "嗯", "ok", "OK", "好的", "。"):
            continue
        # 只取第一句（到句号/问号/感叹号/分号）
        m = _re.match(r"(.{6,60}?)[。！？；]", s)
        one = m.group(1) if m else s[:50]
        who = "他" if role == "user" else "我"
        bullets.append(f"- {who}：{one}")
    if not bullets:
        return ""
    bullets.reverse()
    return "（本地模型不可用，以下是规则抽取的要点）\n" + "\n".join(bullets)



def write_digest(lines: list[str], keep_days: int) -> Path:
    today = date.today()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    header = [
        "# 近三天 · 会话摘要（记忆体 V2.1 实时层）",
        "",
        "> 主题：最近 3 天我跟主人聊了什么（滚动窗口，超期自动滚进 `31-生活.md`）",
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
    # 2026-11-20 修：以前用 b[:12] 字符串比较（"## 2026-11-1" 截断到日就区分不出来），
    # 会把 11-18/11-19 的段误删；改成解析标题里的日期再比。
    def _block_date(block: str):
        m = re.match(r"## (\d{4}-\d{2}-\d{2})", block)
        return m.group(1) if m else None

    kept = []
    sunk = []
    for b in old_blocks:
        if not b.startswith("## "):
            kept.append(b)
            continue
        d = _block_date(b)
        if d is None or d >= cutoff:     # 无日期的手写段，或日期在保留窗口内（含第 3 天）
            kept.append(b)
        else:
            sunk.append((d, b))          # 超期 → 下沉（一句话 + 指针）

    # 下沉：超期段压成一句 ≤60 字，写进 31-生活.md，然后从摘要里删掉（V2.1 ③）
    if sunk:
        life = HOME / ".dsh" / "memory" / "31-生活.md"
        lines_out = []
        for d, block in sunk:
            # 取该段第一条 bullet 作为"发生了什么"，压到 50 字
            body_lines = [ln.strip("- ").strip() for ln in block.splitlines()[1:]
                          if ln.strip().startswith("- ")]
            one = body_lines[0] if body_lines else block.splitlines()[1][:50]
            one = re.sub(r"\s+", " ", one)[:50]
            lines_out.append(f"- 📦 下沉 {d}：{one}（详见当日日志）")
            print(f"  ↓ 下沉 {d}")
        if lines_out:
            with life.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "\n".join(lines_out) + "\n")
            print(f"  已下沉 {len(lines_out)} 段到 31-生活.md")
    # 同一天只留一段：把旧的"今天"段去掉，用新生成的替换（2026-11-20 修）
    kept = [b for b in kept if _block_date(b) != today.isoformat()]
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
            print(f"    {'我' if r=='user' else '鲸鲸'}：{t[:80]}")
        return 0

    # 本地模型可能正被"刷B站"或游戏占着 → 重试 3 次，每次隔 20 秒
    summary = ""
    last_err = None
    for attempt in range(1, 4):
        try:
            summary = local_summary(turns)
            if summary:
                print(f"  ✅ 本地模型摘要完成（第 {attempt} 次尝试）")
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < 3:
            print(f"  … 本地模型忙，20 秒后重试（{attempt}/3）")
            import time as _t
            _t.sleep(20)
    try:
        if not summary:
            raise RuntimeError(last_err or "模型返回空摘要")
    except Exception as e:
        # 模型失败 → 抽取式兜底（明确标注是规则摘要），至少让当天有记录，不再整天跳过
        print(f"  ⚠️ 本地模型不可用（{e}），改用抽取式摘要")
        summary = extractive_summary(turns)
        if not summary:
            print("  ⚠️ 抽取也没抽到内容，跳过本次（保留上一版）")
            return 0
    out = write_digest([summary], a.days)
    print(f"  已写入 {out}（{len(summary.splitlines())} 行摘要）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
