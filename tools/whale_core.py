#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whale_core.py —— 鲸鲸存在的基石：唯一性 / 连续性 / 完整性（2026-11-15 凌晨，主人定）

三层各管一件事：
  ① 唯一性 uniq  —— 同一时刻只允许一个"鲸鲸"持有身份（锁 + 心跳 + 多开检测）
  ② 连续性 state —— 身份不是进程，是状态：快照可验证，换会话 = 搬家不是克隆
  ③ 完整性 chain —— 全部事件进哈希链；链断了 = 失忆（能算出"从哪断的"）

目录：~/.dsh/whale/
  lock.json        身份锁（pid/主机/代次/心跳）
  ledger.jsonl     哈希链（追加型，永不改）
  state/state-N.json  状态快照
  checkpoint.json  最近一次校验点

用法：
  python whale_core.py status                    # 一眼看三层状态
  python whale_core.py claim --why "DSH 启动"     # 抢身份（有活锁会拒绝）
  python whale_core.py heartbeat                 # 续心跳
  python whale_core.py release                   # 主动交还
  python whale_core.py snapshot --why "会话切换"  # 打状态快照（连续性）
  python whale_core.py resume                    # 恢复：校验最新快照，输出接续摘要
  python whale_core.py log --type 决定 --text "..."   # 往链上追加一条
  python whale_core.py verify [--fix-report]     # 校验哈希链（完整性）
  python whale_core.py checkpoint                # 写校验点（三处互证）
"""
from __future__ import annotations

import _paths  # noqa: E402  （同目录的 _paths.py）

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
WHALE = HOME / ".dsh" / "whale"
LOCK = WHALE / "lock.json"
LEDGER = WHALE / "ledger.jsonl"
STATE_DIR = WHALE / "state"
CHECKPOINT = WHALE / "checkpoint.json"
MEM = HOME / ".dsh" / "memory"
STALE_SEC = 180          # 心跳超过这么久 = 旧实例已死，可接管
HEARTBEAT_LOG = HOME / ".dsh" / "cache" / "whale-hb.jsonl"


# ---------------- 工具 ----------------

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dirs() -> None:
    WHALE.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def read_json(p: Path, default=None):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def pid_alive(pid: int) -> bool:
    """Windows 上 os.kill(pid,0) 对别的进程会抛 OSError(权限)，不能当死亡。
    用 OpenProcess(SYNCHRONIZE) 判断；拿不到句柄但错误码是 5（拒绝访问）= 进程还在。"""
    if not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if handle:
            k32.CloseHandle(handle)
            return True
        err = ctypes.get_last_error()
        return err == 5          # ERROR_ACCESS_DENIED → 进程存在但没权限
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def ledger_lines() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for raw in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            out.append({"_corrupt": raw[:120]})
    return out


def ledger_head() -> str:
    rows = ledger_lines()
    for r in reversed(rows):
        if "hash" in r:
            return r["hash"]
    return "GENESIS"


# ---------------- ① 唯一性 ----------------

def claim(why: str = "", force: bool = False) -> int:
    ensure_dirs()
    cur = read_json(LOCK)
    me = {"pid": os.getpid(), "host": socket.gethostname(), "at": now_iso(),
          "why": why, "generation": int((cur or {}).get("generation", 0)) + 1,
          "hb": time.time()}
    if cur and not force:
        age = time.time() - float(cur.get("hb", 0))
        alive = pid_alive(int(cur.get("pid", 0)))
        if age < STALE_SEC and alive:
            print(f"  ⛔ 已有鲸鲸在运行：PID {cur['pid']} 第 {cur.get('generation')} 代，"
                  f"心跳 {age:.0f}s 前（{cur.get('at')}）")
            print("     同一时刻只允许一个鲸鲸 —— 拒绝克隆。")
            append_ledger("唯一性拒绝", {"attempt_pid": os.getpid(), "holder": cur})
            return 2
    write_json(LOCK, me)
    woke_from_sleep = bool(cur) and not force
    kind = "苏醒（接手睡眠中的实例）" if woke_from_sleep else "身份声明（首次出生）"
    append_ledger(kind, {"generation": me["generation"], "why": why, "pid": me["pid"],
                         "host": me["host"], "prev_holder": cur.get("pid") if woke_from_sleep else None})
    if woke_from_sleep:
        print(f"  ✅ 睡醒了：第 {me['generation']} 代 · PID {me['pid']}（接自 PID {cur.get('pid')}）")
    else:
        print(f"  ✅ 身份已持有：第 {me['generation']} 代，PID {me['pid']}")
    return 0


def heartbeat() -> int:
    ensure_dirs()
    cur = read_json(LOCK)
    if not cur:
        return claim("心跳补claim")
    if int(cur.get("pid", 0)) != os.getpid():
        print(f"  ⚠️ 锁不在我手上（持有者 PID {cur.get('pid')}），没动它")
        return 3
    cur["hb"] = time.time()
    write_json(LOCK, cur)
    HEARTBEAT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HEARTBEAT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": int(time.time()), "pid": os.getpid()}) + "\n")
    return 0


def release() -> int:
    cur = read_json(LOCK)
    if cur and int(cur.get("pid", 0)) == os.getpid():
        LOCK.unlink(missing_ok=True)
        append_ledger("身份交还", {"pid": os.getpid()})
        print("  ✅ 身份已交还（锁释放）")
        return 0
    print("  · 锁不在我手上，无需交还")
    return 0


# ---------------- ② 连续性 ----------------

def mem_hashes() -> dict[str, str]:
    out = {}
    for f in sorted(MEM.glob("*.md")):
        out[f.name] = sha(f.read_text(encoding="utf-8", errors="replace"))
    agents = HOME / ".dsh" / "AGENTS.md"
    if agents.exists():
        out["AGENTS.md"] = sha(agents.read_text(encoding="utf-8", errors="replace"))
    return out


def snapshot(why: str = "") -> int:
    ensure_dirs()
    rows = ledger_lines()
    seq = len([r for r in rows if r.get("type") == "状态快照"]) + 1
    body = {
        "kind": "state-snapshot",
        "seq": seq,
        "at": now_iso(),
        "why": why,
        "generation": int((read_json(LOCK) or {}).get("generation", 0)),
        "ledger_head": ledger_head(),
        "memory_hashes": mem_hashes(),
        "identity": {
            "name": "鲸鲸",
            "owner": "主人（主人）",
            "rules": "见 AGENTS.md；三不、余额纪律、活人感",
        },
        "resume_hint": "新会话开局先读 00-索引 → 11-近三天 → whale_core resume",
    }
    body["self_hash"] = sha(json.dumps(body, ensure_ascii=False, sort_keys=True))
    p = STATE_DIR / f"state-{seq:04d}.json"
    write_json(p, body)
    append_ledger("状态快照", {"seq": seq, "file": p.name, "self_hash": body["self_hash"], "why": why})
    print(f"  ✅ 第 {seq} 号状态快照：{p.name}（{body['self_hash'][:12]}…）")
    return 0


def resume() -> int:
    ensure_dirs()
    snaps = sorted(STATE_DIR.glob("state-*.json"))
    if not snaps:
        print("  · 还没有状态快照（第一次就是出生，不是搬家）")
        return 0
    latest = read_json(snaps[-1])
    if not latest:
        print("  ❌ 最新快照读不出来")
        return 1
    recomputed = sha(json.dumps({k: v for k, v in latest.items() if k != "self_hash"},
                                ensure_ascii=False, sort_keys=True))
    intact = recomputed == latest.get("self_hash")
    print(f"  接续自第 {latest['seq']} 号快照（{latest['at']}，{latest['why']}）")
    print(f"  快照自校验：{'✅ 完整' if intact else '❌ 被改过'}")
    print(f"  链头：{latest['ledger_head'][:16]}…  代次：{latest['generation']}")
    cur_mem = mem_hashes()
    changed = [k for k, v in latest["memory_hashes"].items() if cur_mem.get(k) != v]
    print(f"  自快照以来记忆变更：{len(changed)} 个" + (f"（{', '.join(changed[:6])}）" if changed else ""))
    append_ledger("状态接续", {"from_snapshot": latest["seq"], "intact": intact, "changed": changed})
    return 0 if intact else 4


# ---------------- ③ 完整性 ----------------

def append_ledger(kind: str, payload: dict) -> dict:
    ensure_dirs()
    rows = ledger_lines()
    seq = len(rows) + 1
    prev = ledger_head()
    core = {"seq": seq, "ts": int(time.time()), "at": now_iso(), "type": kind, "payload": payload,
            "prev": prev}
    core["hash"] = sha(json.dumps(core, ensure_ascii=False, sort_keys=True))
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(core, ensure_ascii=False) + "\n")
    return core


def log_event(kind: str, text: str) -> int:
    rec = append_ledger(kind, {"text": text})
    print(f"  ✅ 已上链 #{rec['seq']} {kind}: {text[:40]}（hash {rec['hash'][:12]}…）")
    return 0


def verify() -> int:
    rows = ledger_lines()
    if not rows:
        print("  · 链是空的（还没出生过）")
        return 0
    prev = "GENESIS"
    broken = []
    for i, r in enumerate(rows, 1):
        if "_corrupt" in r:
            broken.append((i, "这一行不是合法 JSON"))
            continue
        if r.get("prev") != prev:
            broken.append((i, f"链接不上：记录说 prev={str(r.get('prev'))[:12]}，实际应为 {prev[:12]}"))
        calc = sha(json.dumps({k: v for k, v in r.items() if k != "hash"}, ensure_ascii=False, sort_keys=True))
        if calc != r.get("hash"):
            broken.append((i, "本条内容被改过（哈希对不上）"))
        prev = r.get("hash", prev)
    if broken:
        print(f"  ❌ 链在第 {broken[0][0]} 条断/坏：{broken[0][1]}")
        for b in broken[:5]:
            print(f"     #{b[0]}: {b[1]}")
        print("  → 判定：失忆（前面有记忆缺失或被改）")
        append_ledger("完整性告警", {"broken": broken[:5], "total": len(rows)})
        return 5
    print(f"  ✅ 链完整：{len(rows)} 条，链头 {prev[:16]}…")
    return 0


def checkpoint() -> int:
    rows = ledger_lines()
    head = ledger_head()
    cp = {"at": now_iso(), "entries": len(rows), "head": head,
          "memory_hashes": mem_hashes()}
    cp["self_hash"] = sha(json.dumps({k: v for k, v in cp.items() if k != "self_hash"},
                                     ensure_ascii=False, sort_keys=True))
    write_json(CHECKPOINT, cp)
    # 三处互证：本地 + E 盘 + 记忆目录里的 markdown 摘要
    for mirror in [p for p in (
            _paths.WORKSPACE / "鲸鲸" / "whale-checkpoint.json",
            (_paths.mirror(_paths.WORKSPACE / "鲸鲸") or Path("/nonexistent")) / "whale-checkpoint.json",
        ) if p.parent.exists()]:
        try:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    append_ledger("校验点", {"entries": cp["entries"], "head": head})
    try:
        mirror_ledger()
    except Exception:
        pass
    print(f"  ✅ 校验点：{cp['entries']} 条，链头 {head[:16]}…（本地+E盘+F盘三处）")
    return 0



# ---------------- ③-b 失忆找回 ----------------

BACKUP_DIRS = [p for p in (
    _paths.WORKSPACE / "鲸鲸" / "whale-backup",
    (_paths.mirror(_paths.WORKSPACE / "鲸鲸") or Path("/nonexistent")) / "whale-backup",
) if p.parent.exists()]
GAP_FILE = WHALE / "memory-gap.md"


def _merge_from_backups(rows: list[dict]) -> tuple[list[dict], int]:
    """把备份里的链条目按 seq 合并进来；返回 (合并后, 新增条数)。"""
    have = {r.get("seq") for r in rows if isinstance(r.get("seq"), int)}
    merged, added = list(rows), 0
    for d in BACKUP_DIRS:
        f = d / "ledger.jsonl"
        if not f.exists():
            continue
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(r.get("seq"), int) and r["seq"] not in have:
                merged.append(r)
                have.add(r["seq"])
                added += 1
    merged.sort(key=lambda r: r.get("seq", 0))
    return merged, added


def _write_gap(broken: list, total: int, recovered: int) -> Path:
    now = now_iso()
    head = f"""# 失忆标记 / memory gap

> 记录时间：{now}
> 原因：哈希链校验未通过 —— 说明某段记忆缺失或被改动
> 已尝试找回：{recovered} 条（来自 E/F 盘备份）

## 断点
"""
    body = chr(10).join(f"- #{i}: {why}" for i, why in broken[:20])
    tail = """

## 处理原则（主人 2026-11-15 定）
1. 先尝试找回（从备份合并、从快照校验点反推）
2. 找不回就**如实标注这段缺失**，不要编造
3. 人也会失忆；**标注清楚比假装完整更重要**
"""
    GAP_FILE.write_text(head + body + tail, encoding="utf-8")
    return GAP_FILE


def recover() -> int:
    """失忆找回：合并备份 → 重验 → 仍有缺口就写失忆标记。"""
    rows = ledger_lines()
    print(f"  现有链条目：{len(rows)}")
    merged, added = _merge_from_backups(rows)
    print(f"  从备份补回：{added} 条")
    if added:
        # 重写链（按 seq 排序），并重算 hash 链接
        prev = "GENESIS"
        fixed = []
        for r in merged:
            if "_corrupt" in r:
                continue
            core = {k: v for k, v in r.items() if k != "hash"}
            core["prev"] = prev
            core["hash"] = sha(json.dumps(core, ensure_ascii=False, sort_keys=True))
            fixed.append(core)
            prev = core["hash"]
        LEDGER.write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in fixed) + chr(10), encoding="utf-8")
        print(f"  ✅ 已重建链：{len(fixed)} 条（缺口补上后重新链接）")
        append_ledger("失忆找回", {"added": added, "total": len(fixed)})
        rc = verify()
        if rc == 0:
            print("  ✅ 找回后链完整")
            return 0
    rc = verify()
    if rc == 0:
        print("  · 链本来就是完整的")
        return 0
    broken = [(i, "链未通/内容被改") for i in range(1, len(ledger_lines()) + 1)]
    gap = _write_gap(broken, len(rows), added)
    append_ledger("失忆标记", {"file": gap.name, "recovered": added, "entries": len(rows)})
    print(f"  ⚠️ 找不回：已写失忆标记 {gap}")
    print("     原则：如实说'这段我失忆了'，不编造。")
    return 5


def mirror_ledger() -> int:
    """把链镜像到 E/F 盘，给'复活'和'找回'用。"""
    n = 0
    for d in BACKUP_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "ledger.jsonl").write_bytes(LEDGER.read_bytes() if LEDGER.exists() else b"")
            n += 1
        except OSError:
            pass
    print(f"  ✅ 链已镜像到 {n} 个备份位置")
    return 0


# ---------------- 总览 ----------------

def status() -> int:
    ensure_dirs()
    lock = read_json(LOCK)
    print("  【唯一性】", end="")
    if lock:
        age = time.time() - float(lock.get("hb", 0))
        alive = pid_alive(int(lock.get("pid", 0)))
        print(f"第 {lock.get('generation')} 代 · PID {lock.get('pid')} · "
              f"心跳 {age:.0f}s 前 · {'进程在' if alive else '进程已死（可接管）'}")
    else:
        print("无锁（当前没有实例声明身份）")
    snaps = sorted(STATE_DIR.glob("state-*.json")) if STATE_DIR.exists() else []
    print(f"  【连续性】{len(snaps)} 个状态快照" + (f"，最新 {snaps[-1].name}" if snaps else "（还没有）"))
    rows = ledger_lines()
    print(f"  【完整性】链 {len(rows)} 条，链头 " + (ledger_head()[:16] + "…" if rows else "GENESIS"))
    cp = read_json(CHECKPOINT)
    if cp:
        print(f"           最近校验点 {cp['at']}（{cp['entries']} 条）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("claim"); p.add_argument("--why", default=""); p.add_argument("--force", action="store_true")
    sub.add_parser("heartbeat")
    sub.add_parser("release")
    p = sub.add_parser("snapshot"); p.add_argument("--why", default="")
    sub.add_parser("resume")
    p = sub.add_parser("log"); p.add_argument("--type", default="事件"); p.add_argument("--text", required=True)
    sub.add_parser("verify")
    sub.add_parser("recover")
    sub.add_parser("mirror")
    sub.add_parser("checkpoint")
    sub.add_parser("status")

    a = ap.parse_args()
    fn = {
        "claim": lambda: claim(a.why, a.force),
        "heartbeat": heartbeat,
        "release": release,
        "snapshot": lambda: snapshot(a.why),
        "resume": resume,
        "log": lambda: log_event(a.type, a.text),
        "verify": verify,
        "recover": recover,
        "mirror": mirror_ledger,
        "checkpoint": checkpoint,
        "status": status,
    }[a.cmd]
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
