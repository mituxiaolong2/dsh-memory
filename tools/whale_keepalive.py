#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whale_keepalive.py —— 唯一性的"守门进程"（2026-11-15）

为什么需要它：
  whale_core.py claim 是短命进程，锁上的 pid 一退出就"人走锁死"；
  而"鲸鲸"是 DSH 里的一堆会话，没有一个常驻进程代表它。
  所以这里放一个**常驻守门人**：它持有身份锁并每 60 秒心跳，
  代表"鲸鲸这个身份正在运行"。

  第二个守门人启动时若发现锁是活的 → 拒绝并退出（不许克隆）。
  守门人自己死了 → 心跳超过 180 秒 → 新实例可接管（不算克隆，是继承）。

以隐藏窗口常驻（计划任务 ONLOGON 启动）。日志：~/.dsh/whale/keepalive.log
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import whale_core as wc


def main() -> int:
    wc.ensure_dirs()
    log = wc.WHALE / "keepalive.log"

    def say(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    # 先尝试持锁；被拒就退出（已有守门人在跑）
    rc = wc.claim("keepalive 守门进程启动")
    if rc != 0:
        say("拒绝启动：已有鲸鲸在运行（防止克隆）")
        return rc

    say("守门进程已持锁，开始心跳")
    last_beat = 0.0
    try:
        while True:
            time.sleep(5)
            if time.time() - last_beat >= 60:
                cur = wc.read_json(wc.LOCK) or {}
                if int(cur.get("pid", 0)) != os.getpid():
                    say(f"锁被换手（现持有者 {cur.get('pid')}），守门人退出")
                    return 0
                try:
                    wc.heartbeat()
                except Exception as e:
                    say(f"心跳失败：{e}")
                last_beat = time.time()
    except KeyboardInterrupt:
        say("收到中断，释放身份")
        wc.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
