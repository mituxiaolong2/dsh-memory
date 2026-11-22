# -*- coding: utf-8 -*-
"""model_gate.py — 记忆体本地模型的"按需拉起 + 空闲自动停 + 游戏时延后"

设计（2026-11-17 定，主人选方案 B）：
  · 默认不常驻：平时显存 0
  · 谁要用模型 → 先调 ensure()：探 8095 → 不通就拉起 → 等就绪（约 6 秒）
  · 每次调用写一次时间戳（state/last_used）
  · 看门狗：超过 IDLE_MINUTES 没人用 → 停掉，释放显存
  · 游戏保护：拉起前看显存，已用 > VRAM_BUSY_MB（默认 4096MB）说明在玩游戏 →
    不拉起，登记"待办"，返回 BUSY；等显存降到 VRAM_IDLE_MB（默认 2048MB）以下再跑

用法（CLI）：
  python -X utf8 model_gate.py ensure          # 确保模型可用（游戏时会返回忙碌）
  python -X utf8 model_gate.py wait            # 等到不忙再拉起（可被定时任务调用）
  python -X utf8 model_gate.py stop            # 立即停（立刻还显存）
  python -X utf8 model_gate.py watchdog        # 单次看门狗检查（配合计划任务每 5 分钟跑）
  python -X utf8 model_gate.py status          # 状态（JSON）

返回码：0=就绪 / 3=显存被别人占着（游戏）/ 4=启动失败
"""
from __future__ import annotations

import json
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000  # 子进程不弹黑窗
import sys
import time
import urllib.request
from pathlib import Path

ENGINE_IMG = "ninfer-serve-86.exe"
PORT = 8095
# 引擎位置（2026-11-21 搬到 5.插件）：直接用 Popen 起，别走 .cmd/.vbs——
# 路径里有中文，cmd 按 ANSI 读脚本会解析错，VBS 派生的子进程又会被回收。
# 本地引擎位置（默认按环境变量取，换机器只改环境变量）
ENGINE_ROOT = Path(os.environ.get(
    "DSH_ENGINE_ROOT", Path.home() / "infer-engine"))
ENGINE_EXE = Path(os.environ.get("DSH_ENGINE_EXE", ENGINE_ROOT / "engine" / "ninfer-serve-86.exe"))
ENGINE_MODEL = Path(os.environ.get(
    "DSH_ENGINE_MODEL", ENGINE_ROOT / "models" / "bonsai2_27b_ternary_ptq1_native_mtp.ninfer"))
ENGINE_LOG = Path(os.environ.get("DSH_ENGINE_LOG", ENGINE_ROOT / "logs" / "engine.log"))
ENGINE_ARGS = [
    "--host", "127.0.0.1", "--port", str(PORT), "--model-id", "bonsai27b",
    "--max-context", "65536", "--kv-capacity", "16384", "--kv-dtype", "rk2v4-e8",
    "--host-kv-mib", "1536", "--prefill-chunk", "512", "--spec", "mtp",
    "--draft-tokens", "4", "--default-max-tokens", "4096",
    "--default-reasoning-effort", "none", "--max-concurrency", "1",
    "--max-shared-prefixes", "0", "--no-cuda-graph", "--presence-penalty", "0",
    "--temperature", "0.7", "--top-p", "0.9", "--top-k", "20",
]
BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
STATE.mkdir(exist_ok=True)
LAST_USED = STATE / "last_used.json"
PENDING = STATE / "pending_runs.jsonl"

IDLE_MINUTES = 10          # 空闲多久自动停
VRAM_BUSY_MB = 5120        # 显存高于这个才算忙（本机壁纸/浏览器基线约 5.4G）
VRAM_IDLE_MB = 3072        # 显存低于这个 = 空闲
WAIT_TIMEOUT_MIN = 30      # wait 模式最多等这么久


# ---------------------------------------------------------------- 基础查询
def run(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=timeout,
                              encoding="utf-8", errors="ignore").stdout
    except Exception:
        return ""


def engine_pids() -> list[int]:
    out = run(["tasklist", "/FI", f"IMAGENAME eq {ENGINE_IMG}", "/FO", "CSV", "/NH"])
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("ninfer"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def port_open() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def vram_used_mb() -> int:
    out = run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return int(out.strip().splitlines()[0])
    except Exception:
        return 0


def touch() -> None:
    LAST_USED.write_text(json.dumps({"ts": time.time(), "iso": time.strftime("%F %T")}),
                         encoding="utf-8")


def last_used_age_min() -> float:
    if not LAST_USED.exists():
        return 1e9
    try:
        return (time.time() - json.loads(LAST_USED.read_text(encoding="utf-8"))["ts"]) / 60
    except Exception:
        return 1e9


# ---------------------------------------------------------------- 核心动作
def start_engine() -> bool:
    """拉起引擎并等端口就绪（直接 Popen，避开中文路径的 cmd 解析问题）"""
    if not ENGINE_EXE.exists():
        print(f"启动失败：引擎不存在 {ENGINE_EXE}")
        return False
    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(ENGINE_EXE.parent) + os.pathsep + env.get("PATH", "")
    env.update({
        "NINFER_KV_WINDOW": "8192", "NINFER_KV_RETRIEVE": "4096", "NINFER_KV_RING": "1",
        "NINFER_HOST_PAGEABLE": "1", "NINFER_KV_REUSE_HOSTBACKED": "1",
        "NINFER_TERNARY_PTQ1_FAST": "1",
    })
    with open(ENGINE_LOG, "ab") as logf:
        subprocess.Popen(
            [str(ENGINE_EXE), str(ENGINE_MODEL), *ENGINE_ARGS],
            cwd=str(ENGINE_EXE.parent), env=env,
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | 0x00000008,  # DETACHED_PROCESS
        )
    for _ in range(60):
        time.sleep(1)
        if port_open():
            touch()
            return True
    return False


def stop_engine() -> bool:
    pids = engine_pids()
    for pid in pids:
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
    for _ in range(10):
        time.sleep(0.5)
        if not engine_pids():
            return True
    return not engine_pids()


def game_running() -> str:
    """有没有真的游戏在跑（显存高不一定是游戏，壁纸/浏览器也会占）。"""
    try:
        out = run(["tasklist", "/FO", "CSV", "/NH"])
    except Exception:
        return ""
    for line in out.splitlines():
        low = line.lower()
        for key in ("deltaforce", "steam.exe", "wegame", "valorant", "cs2.exe", "genshin"):
            if key in low:
                return line.split(",")[0].strip('"')
    return ""


def ensure(wait: bool = False) -> int:
    """确保模型可用。游戏（显存高）时不拉起，返回 3；wait=True 则等到空闲。"""
    if port_open():
        touch()
        return 0
    used = vram_used_mb()
    game = game_running()
    if used > VRAM_BUSY_MB:
        with open(PENDING, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "iso": time.strftime("%F %T"),
                                 "vram_mb": used, "reason": ("game" if game else "vram_busy")}) + "\n")
        why = f"检测到游戏 {game}" if game else "显存被其它程序占用（非游戏）"
        print(f"显存占用 {used}MB（> {VRAM_BUSY_MB}MB，{why}）→ 延后启动，已登记待办")
        if not wait:
            return 3
        t0 = time.time()
        while (time.time() - t0) / 60 < WAIT_TIMEOUT_MIN:
            time.sleep(30)
            if port_open():
                touch()
                return 0
            if vram_used_mb() < VRAM_IDLE_MB:
                break
        if vram_used_mb() >= VRAM_IDLE_MB:
            print(f"等了 {WAIT_TIMEOUT_MIN} 分钟还没空闲，放弃本次")
            return 3
        print("显存已回落，开始启动模型…")
    if start_engine():
        print(f"模型已就绪（显存 {vram_used_mb()}MB）")
        return 0
    print(f"启动失败：看 {ENGINE_LOG}")
    return 4


def watchdog() -> int:
    """空闲检查：模型在跑但太久没被用 → 停掉

    注意：这个函数由 Windows 计划任务每 5 分钟调用一次，用的是 pythonw（无控制台），
    所以结果要写日志，否则看不到它有没有跑。
    """
    log_file = STATE / "watchdog.log"
    def note(msg: str) -> None:
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%F %T')} {msg}\n")
        except OSError:
            pass

    if not engine_pids():
        note("模型没在跑，跳过")
        print("模型没在跑，无需处理")
        return 0
    age = last_used_age_min()
    if age >= IDLE_MINUTES:
        stop_engine()
        note(f"空闲 {age:.1f} 分钟 ≥ {IDLE_MINUTES} → 已停止，显存 {vram_used_mb()}MB")
        print(f"空闲 {age:.1f} 分钟（阈值 {IDLE_MINUTES}）→ 已停止，显存 {vram_used_mb()}MB")
        return 0
    note(f"空闲 {age:.1f} 分钟，保留")
    print(f"空闲 {age:.1f} 分钟，继续保留")
    return 0


def status() -> int:
    data = {
        "running": bool(engine_pids()),
        "pids": engine_pids(),
        "port_open": port_open(),
        "vram_used_mb": vram_used_mb(),
        "vram_busy_threshold_mb": VRAM_BUSY_MB,
        "vram_idle_threshold_mb": VRAM_IDLE_MB,
        "idle_minutes": IDLE_MINUTES,
        "last_used_age_min": round(last_used_age_min(), 1) if last_used_age_min() < 1e8 else None,
        "game_active": vram_used_mb() > VRAM_BUSY_MB,
        "pending_runs": sum(1 for _ in open(PENDING, encoding="utf-8")) if PENDING.exists() else 0,
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "ensure":
        return ensure(wait=False)
    if cmd == "wait":
        return ensure(wait=True)
    if cmd == "stop":
        ok = stop_engine()
        print(f"已停止｜显存 {vram_used_mb()}MB" if ok else "停止失败")
        return 0 if ok else 4
    if cmd == "watchdog":
        return watchdog()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
