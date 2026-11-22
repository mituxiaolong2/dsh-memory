# -*- coding: utf-8 -*-
"""统一路径来源：所有工具都从这里取目录，方便换环境 / 脱敏发布。

环境变量（都可选）：
  DSH_HOME       DSH 的配置目录，默认 ~/.dsh
  DSH_WORKSPACE  工作区根目录，默认 ~/DSH_work
  DSH_REPORTS    报告输出目录，默认 <workspace>/3.产出文档
  DSH_MIRROR     异盘镜像目录（备份用），默认不启用
  DSH_EXAM_DIR   考编资料目录（可选，只给提醒类脚本用）
"""
from __future__ import annotations

import os
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser()
MEMORY_DIR = DSH_HOME / "memory"
SESSIONS_DIR = DSH_HOME / "sessions"

WORKSPACE = Path(os.environ.get("DSH_WORKSPACE", Path.home() / "DSH_work")).expanduser()
REPORTS_DIR = Path(os.environ.get("DSH_REPORTS", WORKSPACE / "3.产出文档")).expanduser()
TOOLS_DIR = Path(__file__).resolve().parent

_mirror = os.environ.get("DSH_MIRROR", "").strip()
MIRROR_DIR = Path(_mirror).expanduser() if _mirror else None

_exam = os.environ.get("DSH_EXAM_DIR", "").strip()
EXAM_DIR = Path(_exam).expanduser() if _exam else None


def mirror(path: Path) -> Path | None:
    """把一个本地目录映射到异盘镜像（没配 DSH_MIRROR 就返回 None）。"""
    if MIRROR_DIR is None:
        return None
    return MIRROR_DIR / path.name
