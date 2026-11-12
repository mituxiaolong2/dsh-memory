#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context_import.py —— 把 DSH 会话上下文整理成「记忆架构」草稿（V1.2 特性 A）

用途：给"只有会话、没有记忆架构"的 DSH 用户做迁移。
      读会话 → 去噪 → 按记忆架构分类 → 生成草稿到预览目录 → 用户审阅后再合并。

安全：
  · 只读会话文件，绝不改写原会话
  · 只写 --out 指定的预览目录，**绝不碰 ~/.dsh/memory/**
  · 每条带来源标记，不确定的标「待确认」

用法：
  python context_import.py --list                          # 列出所有会话
  python context_import.py --session <id前缀> --out <目录>   # 导入一个会话
  python context_import.py --all --out <目录>                # 导入全部会话
  python context_import.py --session <id> --only-user        # 只要用户说的话
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import compression.zstd as zstd

SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"

# 噪声前缀：这些内容不是"记忆"
NOISE_PREFIXES = (
    "<system-reminder>",
    "Current runtime context",
    "Time sampled",
    "Your parent agent id",
    "The user changed the approval policy",
    "[SCHEDULE",
    "reminders_json:",
)

# 分类关键词（按角色分别判断，避免把助手的工作汇报误判成"约定"）
CONV_KW = ["不要", "必须", "禁止", "记住", "以后都", "每次", "一律", "别用", "别把", "规则", "约定", "红线", "只能"]
SELF_KW = ["我今年", "我叫", "我是", "我的", "我住", "生日", "家里", "父母", "我妈", "我爸", "老婆", "对象",
           "习惯", "喜欢", "讨厌", "怕", "在意", "房贷", "工资", "岁"]
LESSON_KW = ["报错", "失败", "错误", "踩坑", "坑", "异常", "exit code", "timeout", "超时", "根因", "修复", "修好", "教训"]
MILESTONE_KW = ["上线", "立项", "发布", "第一天", "里程碑", "搞定了", "创建了"]

CATEGORY_TITLES = {
    "10-主人.md": "关于这个人（从会话里抽取）",
    "20-项目与工作区.md": "项目与状态（从会话里抽取）",
    "30-约定.md": "约定与红线（从会话里抽取）",
    "40-教训.md": "教训（从会话里抽取）",
    "50-时间线.md": "时间线（从会话里抽取）",
    "90-遗忘.md": "未归类 / 过期内容（从会话里抽取）",
}


def ts_to_date(ms: int | None) -> str:
    if not ms:
        return "未知日期"
    try:
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        return "未知日期"


def clean_text(t: str) -> str | None:
    """去掉噪声与运行时注入，返回可用文本或 None。"""
    s = (t or "").strip()
    if not s:
        return None
    for p in NOISE_PREFIXES:
        if s.startswith(p):
            return None
    # 去掉内嵌的 system-reminder 块
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s, flags=re.S)
    # 去掉技能正文注入块
    s = re.sub(r"<skill_content[^>]*>.*?</skill_content>", "", s, flags=re.S)
    s = re.sub(r"<skill_resources[^>]*>.*?</skill_resources>", "", s, flags=re.S)
    # 去掉渠道尾巴（微信渠道会往用户消息后面追加说明）
    s = re.sub(r"\[渠道：[^\]]+\][\s\S]*$", "", s)
    # 去掉时间戳注入行
    s = re.sub(r"^Time sampled .*$", "", s, flags=re.M)
    # 去掉纯附件路径行
    s = re.sub(r"^\[(语音|图片)\][\s\S]*$", "", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) < 8:
        return None
    return s


def extract_events(path: Path) -> tuple[str, list[dict]]:
    """返回 (会话标题, 事件列表[{date, role, text}])"""
    raw = zstd.decompress(path.read_bytes()).decode("utf-8", "replace")
    title = ""
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = o.get("type")
        date = ts_to_date(o.get("time"))
        if typ == "session/title":
            d = o.get("data") or {}
            title = d.get("title") or title
            continue
        if typ not in ("user/message", "assistant/message"):
            continue
        role = "user" if typ == "user/message" else "assistant"
        data = o.get("data") or {}
        # user: data.content；assistant: data.message.content（含 reasoning / tool-call，只取 text）
        content = data.get("content")
        if content is None:
            content = (data.get("message") or {}).get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                # 只收纯文本；reasoning 与 tool-call 属于过程，不入记忆
                if c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
        for t in texts:
            c = clean_text(t)
            if c and not is_ack(c) and not (role == "assistant" and is_meta(c)):
                events.append({"date": date, "role": role, "text": c})
    return title or path.parent.name, events


def classify(role: str, text: str) -> str:
    """按角色分类：用户的意图/偏好/约定 vs 助手的工作结果/教训。"""
    if role == "assistant":
        # 助手的产出多数是"做了什么/踩了什么坑"
        if any(k in text for k in LESSON_KW):
            return "40-教训.md"
        return "20-项目与工作区.md"
    # 用户
    if any(k in text for k in CONV_KW):
        return "30-约定.md"
    if any(k in text for k in SELF_KW):
        return "10-主人.md"
    if any(k in text for k in MILESTONE_KW):
        return "50-时间线.md"
    if len(re.sub(r"\s+", "", text)) < 12:
        return "90-遗忘.md"
    return "20-项目与工作区.md"


ACK_WORDS = ("好的", "收到", "明白了", "已办完", "已完成", "完成了", "OK", "ok")

def shorten(text: str, limit: int = 160) -> str:
    """压成一句：取第一句/第一行，压空白，超长截断。"""
    one = re.sub(r"\s+", " ", text).strip()
    # 取第一个句末标点之前
    m = re.match(r"^(.{6,}?[。！？!?])", one)
    if m and len(m.group(1)) < 120:
        one = m.group(1)
    return one if len(one) <= limit else one[:limit] + "…"

META_PREFIX = ("我先", "让我", "我来", "正在", "马上", "接下来我", "现在开始", "先看", "先读")

def is_ack(text: str) -> bool:
    t = re.sub(r"\s+", "", text)
    return len(t) <= 8 and any(w in t for w in ACK_WORDS)

def is_meta(text: str) -> bool:
    """助手的"过程性开场白"，不是结论，不入记忆。"""
    s = text.strip()
    if s.startswith(META_PREFIX):
        return True
    return len(s) < 40


def list_sessions() -> list[dict]:
    out = []
    for p in SESSIONS_ROOT.rglob("session.v4.jsonl.zstd"):
        title, events = extract_events(p)
        sid = p.parent.name
        out.append({
            "path": p, "id": sid, "title": title,
            "events": len(events),
            "user": sum(1 for e in events if e["role"] == "user"),
            "assistant": sum(1 for e in events if e["role"] == "assistant"),
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    out.sort(key=lambda x: -x["events"])
    return out


def build_drafts(events: list[dict], session_id: str, title: str) -> dict[str, list[str]]:
    drafts: dict[str, list[str]] = {}
    seen: set[str] = set()
    for e in events:
        target = classify(e["role"], e["text"])
        body = shorten(e["text"])
        key = re.sub(r"\s+", "", body)
        if key in seen:          # 完全重复的内容只留一条
            continue
        seen.add(key)
        line = f"- {body} （{e['role']}）· 来源：{session_id} @ {e['date']}"
        drafts.setdefault(target, []).append(line)
    return drafts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--session", default="", help="session id 前缀")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="", help="输出目录（预览）")
    ap.add_argument("--only-user", action="store_true", help="只导入用户说的话")
    ap.add_argument("--per-file-limit", type=int, default=120, help="每个草稿文件最多几条（默认 120）")
    args = ap.parse_args()

    if args.list:
        rows = list_sessions()
        print(f"=== 会话列表（{len(rows)} 个）===\n")
        print(f"  {'会话ID':<38} {'事件':>6} {'用户':>5} {'助手':>5} {'大小':>8}  标题")
        for r in rows:
            print(f"  {r['id']:<38} {r['events']:>6} {r['user']:>5} {r['assistant']:>5} {r['size_kb']:>7.1f}K  {r['title'][:28]}")
        return 0

    if not (args.session or args.all):
        print("请指定 --list / --session <id> / --all", file=sys.stderr)
        return 2

    rows = list_sessions()
    if args.session:
        rows = [r for r in rows if r["id"].startswith(args.session) or r["id"].replace("session-", "").startswith(args.session)]
        if not rows:
            print(f"没找到会话：{args.session}", file=sys.stderr)
            return 1

    if not args.out:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path.home() / ".dsh" / "memory-import-preview" / stamp
    else:
        out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_events: list[dict] = []
    report_rows = []
    for r in rows:
        sess_id = r["id"].replace("session-", "")[:12]
        _title, events = extract_events(r["path"])
        if args.only_user:
            events = [e for e in events if e["role"] == "user"]
        all_events.extend([{**e, "sess": sess_id} for e in events])
        report_rows.append((sess_id, r["title"], len(events), r["path"]))

    drafts = build_drafts(all_events, "多会话", "")
    # 用真实会话 id 重写来源标记
    for target in list(drafts):
        drafts[target] = [d.replace("来源：多会话", "来源：会话") for d in drafts[target]]

    written = 0
    for target, lines in drafts.items():
        if target == "00-索引.md":
            continue
        limit = args.per_file_limit
        body = "\n".join(lines[:limit])
        more = "" if len(lines) <= limit else f"\n\n> ⚠️ 还有 {len(lines)-limit} 条未列出（超 --per-file-limit）"
        content = (
            f"# {target} · 导入草稿\n\n"
            f"> 由 context_import.py 从会话上下文生成，**尚未合并进记忆库**。\n"
            f"> 请逐条审阅：留的保留、不要的删掉，确认后再合并。\n\n"
            f"## {CATEGORY_TITLES.get(target, target)}\n\n{body}{more}\n"
        )
        (out_dir / target).write_text(content, encoding="utf-8")
        written += 1

    # 索引草稿
    idx_lines = ["# 00 · 导入的索引草稿\n",
                 "> 每条一句话 + 指向文件。合并前请压缩成一句话。\n"]
    for target, lines in drafts.items():
        if target == "00-索引.md" or not lines:
            continue
        idx_lines.append(f"\n## {target}")
        for d in lines[:30]:
            idx_lines.append(f"- {shorten(d, 90)} → `{target}`")
    (out_dir / "00-索引-草稿.md").write_text("\n".join(idx_lines) + "\n", encoding="utf-8")

    # 导入报告
    sensitive_kw = ["密码", "token", "key", "身份证", "银行卡", "手机号", "住址", "sk-", "github_pat"]
    sens = []
    for e in all_events:
        low = e["text"].lower()
        for k in sensitive_kw:
            if k in low:
                sens.append((k, shorten(e["text"], 60), e["date"]))
                break

    rep = ["# 导入报告\n",
           f"- 生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
           f"- 来源会话：{len(rows)} 个",
           f"- 抽取事件：{len(all_events)} 条（用户 {sum(1 for e in all_events if e['role']=='user')} / 助手 {sum(1 for e in all_events if e['role']=='assistant')}）",
           f"- 生成草稿文件：{written} 个",
           "", "## 来源会话明细", ""]
    for sid, title, n, path in report_rows:
        rep.append(f"- `{sid}` {title[:30]} — {n} 条 · {path.name}")
    rep += ["", "## 分类统计", ""]
    for target, lines in sorted(drafts.items()):
        rep.append(f"- `{target}`：{len(lines)} 条")
    rep += ["", "## ⚠️ 疑似敏感项（合并前请处理）", ""]
    if sens:
        for k, txt, date in sens[:20]:
            rep.append(f"- [{k}] {txt} （{date}）")
    else:
        rep.append("- 未发现明显敏感词")
    rep += ["", "## 下一步", "",
            "1. 逐文件审阅，删除不要的条目",
            "2. 把留下的条目合并进 `~/.dsh/memory/`（**先打架构快照**）",
            "3. 把 `00-索引-草稿.md` 压缩成一句话式索引",
            "4. 不需要就整个删掉预览目录，**不会影响原会话和现有记忆**"]
    (out_dir / "_导入报告.md").write_text("\n".join(rep) + "\n", encoding="utf-8")

    print(f"=== 导入草稿已生成 ===")
    print(f"  输出目录：{out_dir}")
    print(f"  事件 {len(all_events)} 条 → 草稿 {written} 个文件")
    print()
    for target, lines in sorted(drafts.items()):
        print(f"    {target:<22} {len(lines):>4} 条")
    print()
    print(f"  报告：{out_dir / '_导入报告.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
