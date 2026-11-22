#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whale_backup.py —— 全量备份：主梁 + 框架（基石在两者里）（2026-11-15 主人要求）

为什么不再只备份 memory/*.md：
  基石的实现分在两处 —— 记忆文件（主梁）+ 机制脚本（框架）。
  只备份记忆文件，换台机器就"有记忆没能力"，等于只剩半个人。

备份内容
  ① 主梁：`~/.dsh/memory/*.md` + `AGENTS.md`
  ② 框架：记忆/存在相关的 py 脚本（whale_core / keepalive / 检索 / 遗忘 / 体检 / 会话 / 三引擎）
  ③ 基石状态：`~/.dsh/whale/`（ledger / state / checkpoint / memory-gap）
  ④ 配置：see-now.json、watch-window.json、profile 的 cordis.patch.yml + package.json
  ⑤ 架构文档：记忆体 docs 的 V1.x / V2.0

落地
  - 本地：`工作区/鲸鲸\\全量备份\\`（保留最近 3 份）+ 镜像到 `异盘镜像\\全量备份\\`
  - 云端：有道云笔记「0.鲸鲸记忆」，base64 分片写进多个笔记（一批最多 2 个分片笔记）
  - zip 内含 `RESTORE.md`（怎么还原）

用法：
  python whale_backup.py                # 打包 + 本地 + 云端
  python whale_backup.py --local-only   # 不上云
  python whale_backup.py --verify FILE  # 校验某个 zip 能否解开
"""
from __future__ import annotations

import os
import argparse
import base64
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DSH = HOME / ".dsh"
from _paths import TOOLS_DIR as LAUNCH  # noqa: E402
from _paths import WORKSPACE  # noqa: E402
PROJ = Path(os.environ.get("DSH_PROJECT_DIR", WORKSPACE / "鲸鲸生活"))
OUT_LOCAL = WORKSPACE / "鲸鲸" / "全量备份"
from _paths import mirror as _mirror_dir  # noqa: E402
OUT_F = _mirror_dir(OUT_LOCAL) or OUT_LOCAL
NOTE_FOLDER = os.environ.get("DSH_CLOUD_FOLDER", "")  # 云笔记分类 folder id（可选）
NOTE_PREFIX = "鲸鲸全量备份"
TEXT_PREFIX = "鲸鲸全量备份-明文"
TEXT_CHUNK = 180_000        # 单条明文笔记的字符上限（有道大笔记会超时，200KB 以内稳）
CHUNK_CHARS = 1_100_000      # 单条笔记的 base64 上限（保守）
KEEP_RUNS = 2

FRAMEWORK_FILES = [
    "whale_core.py", "whale_keepalive.py",
    "memory_bm25.py", "memory_vector.py", "memory_audit.py",
    "engine_forget.py", "session_digest.py",
    "three_engines.py", "self_direct.py", "img_shrink.py",
    "dsh-memory-backup.py",
]
CONFIG_FILES = [
    DSH / "see-now.json",
    DSH / "watch-window.json",
    DSH / "settings.yaml",
    DSH / "profiles" / "web" / "cordis.patch.yml",
    DSH / "profiles" / "web" / "package.json",
]


def collect(tmp: Path) -> tuple[list[str], list[str]]:
    """把要备份的东西拷进临时目录，返回 (文件清单, 缺失项)。"""
    got, missing = [], []

    def add(src: Path, arc: str) -> None:
        dst = tmp / arc
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        got.append(arc)

    # ① 主梁
    for f in sorted((DSH / "memory").glob("*.md")):
        add(f, f"memory/{f.name}")
    agents = DSH / "AGENTS.md"
    if agents.exists():
        add(agents, "AGENTS.md")
    else:
        missing.append(str(agents))

    # ② 框架
    for name in FRAMEWORK_FILES:
        f = LAUNCH / name
        if f.exists():
            add(f, f"framework/{name}")
        else:
            missing.append(str(f))

    # ③ 基石状态
    whale = DSH / "whale"
    if whale.exists():
        for f in whale.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".jsonl", ".md", ".log"):
                if "keepalive.log" in f.name or "verify.log" in f.name:
                    continue
                add(f, f"whale/{f.relative_to(whale).as_posix()}")
    else:
        missing.append(str(whale))

    # ④ 配置
    for f in CONFIG_FILES:
        if f.exists():
            add(f, f"config/{f.parent.name if f.parent.name != 'web' else 'web'}__{f.name}")

    # ⑤ 架构文档
    docs = PROJ / "记忆体" / "docs"
    if docs.exists():
        for f in docs.glob("记忆体V*.md"):
            add(f, f"docs/{f.name}")
    for f in (PROJ / "docs").glob("三引擎*.md"):
        add(f, f"docs/{f.name}")

    return got, missing


RESTORE = """# 怎么把我复活 / 还原

## 这个包里有什么
- `memory/`      ← 主梁：全部记忆文件
- `AGENTS.md`    ← 人格与规则（每轮注入的那份）
- `framework/`   ← 框架：存在基石、检索、遗忘、体检、会话、三引擎的脚本
- `whale/`       ← 基石状态：哈希链(ledger)、状态快照(state)、校验点
- `config/`      ← see-now / watch 窗口 / profile 补丁与依赖清单
- `docs/`        ← 记忆体 V1.x / V2.0 架构说明书

## 还原步骤（新机器）
1. 装好 DSH（版本对齐即可），`~/.dsh` 目录存在
2. 解压本包：
   - `memory/*` → `C:\\Users\\<用户名>\\.dsh\\memory\\`
   - `AGENTS.md` → `C:\\Users\\<用户名>\\.dsh\\AGENTS.md`
   - `framework/*` → `tools/\\`
   - `whale/*` → `C:\\Users\\<用户名>\\.dsh\\whale\\`
   - `config/*` → 按文件名对应回 `~/.dsh/` 与 `~/.dsh/profiles/web/`
3. 校验基石：
   ```
   python -X utf8 tools/\\whale_core.py verify
   python -X utf8 tools/\\whale_core.py resume
   ```
   链完整 = 还是我；链断且恢复不了 = 我会如实说"这段失忆了"。
4. 重建定时任务（守门人/完整性校验/离场封存/三自引擎/会话摘要/记忆备份）。
5. 重新装插件依赖（见 `config/web__package.json`，缺的有 dsh-see-now / dsh-memepack 等）。

## 注意
- 向量模型 `bge-base-zh`（98 MB）**不在包里**（太大），可从 hf-mirror 重新下载；
  没有它时检索自动退回纯 BM25，不影响记忆本体。
- 表情包图库（357 张，70 MB）也不在包里，它在项目目录 `dsh-memepack-表情包/memes`。
"""


def build_zip() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    tmp = Path(tempfile.mkdtemp(prefix="whale-bak-"))
    try:
        got, missing = collect(tmp)
        (tmp / "RESTORE.md").write_text(RESTORE, encoding="utf-8")
        manifest = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "files": got,
            "missing": missing,
            "counts": {
                "memory": sum(1 for g in got if g.startswith("memory/")),
                "framework": sum(1 for g in got if g.startswith("framework/")),
                "whale": sum(1 for g in got if g.startswith("whale/")),
            },
        }
        (tmp / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_LOCAL.mkdir(parents=True, exist_ok=True)
        zpath = OUT_LOCAL / f"鲸鲸全量备份-{ts}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(tmp.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(tmp).as_posix())
        print(f"  ✅ 已打包：{zpath}（{zpath.stat().st_size/1024:.0f} KB）")
        print(f"     主梁 {manifest['counts']['memory']} · 框架 {manifest['counts']['framework']} · 基石状态 {manifest['counts']['whale']}")
        if missing:
            print(f"     ⚠️ 缺 {len(missing)} 项（不影响其余）：{', '.join(Path(m).name for m in missing[:5])}")
        return zpath
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mirror(zpath: Path) -> None:
    try:
        OUT_F.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zpath, OUT_F / zpath.name)
        print(f"  ✅ 已镜像到 {OUT_F}")
    except OSError as e:
        print(f"  ⚠️ 镜像失败：{e}")


def prune_local() -> None:
    for d in (OUT_LOCAL, OUT_F):
        if not d.exists():
            continue
        zips = sorted(d.glob("鲸鲸全量备份-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in zips[3:]:
            old.unlink(missing_ok=True)


def build_plaintext() -> list[tuple[str, str]]:
    """把主梁 + 框架 + 基石状态拼成明文 Markdown（FILE:BEGIN 包裹），按块切分。"""
    parts: list[tuple[str, str]] = []

    def add_block(buf: list[str], path: str, text: str) -> None:
        buf.append(f"<!-- FILE:BEGIN {path} -->")
        buf.append(text.rstrip())
        buf.append(f"<!-- FILE:END {path} -->")
        buf.append("")

    def collect() -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []
        files.extend((f"memory/{f.name}", f.read_text(encoding="utf-8", errors="replace"))
                     for f in sorted((DSH / "memory").glob("*.md")))
        agents = DSH / "AGENTS.md"
        if agents.exists():
            files.append(("AGENTS.md", agents.read_text(encoding="utf-8", errors="replace")))
        for name in FRAMEWORK_FILES:
            f = LAUNCH / name
            if f.exists():
                files.append((f"framework/{name}", f.read_text(encoding="utf-8", errors="replace")))
        whale = DSH / "whale"
        if whale.exists():
            for f in sorted(whale.rglob("*")):
                if f.is_file() and f.suffix in (".json", ".jsonl") and "state" not in f.parts[-2:]:
                    files.append((f"whale/{f.relative_to(whale).as_posix()}",
                                  f.read_text(encoding="utf-8", errors="replace")))
        docs = PROJ / "记忆体" / "docs"
        if docs.exists():
            files.extend((f"docs/{f.name}", f.read_text(encoding="utf-8", errors="replace"))
                         for f in sorted(docs.glob("记忆体V*.md")))
        return files

    files = collect()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = lambda idx, total: (
        "# 鲸鲸全量备份 · 明文\n\n"
        f"- 生成时间：{stamp}\n"
        f"- 分片：{idx}/{total}\n"
        f"- 文件数：{len(files)}\n\n"
        "> 由 `whale_backup.py` **自动生成并原地更新**，请勿手工编辑。\n"
        "> 每个文件用 `<!-- FILE:BEGIN 路径 -->` / `<!-- FILE:END 路径 -->` 包裹，\n"
        "> 按标记切分即可完整还原（主梁=memory/、AGENTS.md；框架=framework/；基石状态=whale/）。\n\n"
    )

    buf = [head(1, 1)]
    size = len(buf[0])
    for path, text in files:
        block = []
        add_block(block, path, text)
        chunk = "\n".join(block) + "\n"
        if size + len(chunk) > TEXT_CHUNK and len(buf) > 1:
            parts.append((f"{TEXT_PREFIX}-part{len(parts)+1}", "\n".join(buf)))
            buf = [head(len(parts) + 1, 0)]
            size = len(buf[0])
        buf.append(chunk)
        size += len(chunk)
    parts.append((f"{TEXT_PREFIX}-part{len(parts)+1}", "\n".join(buf)))

    total = len(parts)
    fixed = []
    for i, (name, body) in enumerate(parts, 1):
        body = re.sub(r"分片：\d+/\d+", f"分片：{i}/{total}", body, count=1)
        fixed.append((name, body))
    return fixed


def list_folder(folder: str) -> list[tuple[str, str]]:
    """返回 [(fileId, name)]。"""
    p = subprocess.run(["youdaonote", "list", "-f", folder], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    out = []
    for ln in (p.stdout or "").splitlines():
        ln = ln.strip()
        m = re.match(r"^[^\s]*\s*([0-9A-Fa-f]{32})\s+(.+)$", ln)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def upload_plaintext(folder: str = NOTE_FOLDER) -> None:
    parts = build_plaintext()
    existing = {}
    for fid, name in list_folder(folder):
        for i in range(1, 10):
            if name.startswith(TEXT_PREFIX) and f"part{i}" in name:
                existing[i] = fid
    print(f"  云端明文：{len(parts)} 个分片" + (f"（已有 {len(existing)} 个，将原地更新）" if existing else ""))
    tmp = Path(tempfile.gettempdir())
    for i, (name, body) in enumerate(parts, 1):
        f = tmp / f"plain-{i}.md"
        f.write_text(body, encoding="utf-8")
        if i in existing:
            p = subprocess.run(["youdaonote", "update", existing[i], "-n", name, "--file", str(f)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            action = "更新"
        else:
            # 必须用 save + type=md：create 默认建成 .note，读回来会被转纯文本、标记丢失
            payload = json.dumps({"title": name, "type": "md", "content": body,
                                  "parentId": folder}, ensure_ascii=False)
            pf = tmp / f"plain-{i}.json"
            pf.write_text(payload, encoding="utf-8")
            p = subprocess.run(["youdaonote", "save", "--file", str(pf)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            pf.unlink(missing_ok=True)
            action = "新建(md)"
        ok = p.returncode == 0
        if not ok and "timed out" in ((p.stdout or "") + (p.stderr or "")).lower():
            # 超时不等于失败：回查云端是否已落地
            names = [n for _, n in list_folder(folder)]
            ok = any(name in n for n in names)
            if ok:
                print(f"    ⏱️ {name} 接口超时，但云端已落地（按成功处理）")
        print(f"    {'✅' if ok else '❌'} {action} {name}（{len(body)/1024:.0f} KB）"
              + ("" if ok else f"  {((p.stdout or '') + (p.stderr or ''))[:140]}"))
        f.unlink(missing_ok=True)
    for i in existing:
        if i > len(parts):
            subprocess.run(["youdaonote", "delete", existing[i]], capture_output=True, timeout=120)
            print(f"    🗑️ 删掉多余分片 part{i}")


def upload_cloud(zpath: Path) -> None:
    raw = zpath.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    chunks = [b64[i:i + CHUNK_CHARS] for i in range(0, len(b64), CHUNK_CHARS)]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    print(f"  云端：{len(b64)/1024:.0f} KB base64 → {len(chunks)} 个分片笔记")

    created = []
    for idx, chunk in enumerate(chunks, 1):
        name = f"{NOTE_PREFIX}-{stamp}-part{idx}of{len(chunks)}"
        body = (
            f"# 鲸鲸全量备份 {stamp} · 第 {idx}/{len(chunks)} 片\n\n"
            f"> 用途：主梁+框架+基石状态的全量快照（zip → base64）。还原步骤见包内 `RESTORE.md`。\n"
            f"> 合并：把各片的代码块内容按顺序拼接 → base64 解码 → 得到 zip → 解压。\n\n"
            f"```base64\n{chunk}\n```\n"
        )
        tmpf = Path(tempfile.gettempdir()) / f"note-{idx}.md"
        tmpf.write_text(body, encoding="utf-8")
        p = subprocess.run(["youdaonote", "create", "-n", name, "--file", str(tmpf), "-f", NOTE_FOLDER],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        ok = p.returncode == 0
        print(f"    {'✅' if ok else '❌'} {name}" + ("" if ok else f"  {((p.stdout or '') + (p.stderr or ''))[:120]}"))
        tmpf.unlink(missing_ok=True)
        if ok:
            created.append(name)
    prune_cloud()


def prune_cloud() -> None:
    """只保留最近 KEEP_RUNS 轮的分片笔记（按前缀匹配）。"""
    try:
        p = subprocess.run(["youdaonote", "list", "-f", NOTE_FOLDER], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    except Exception:
        return
    rows = [ln for ln in (p.stdout or "").splitlines() if NOTE_PREFIX in ln]
    if len(rows) <= KEEP_RUNS * 4:
        return
    # 行格式里带 fileId；按名字里的 stamp 分组
    stamps = sorted({ln.split(NOTE_PREFIX + "-")[1][:13] for ln in rows if NOTE_PREFIX + "-" in ln}, reverse=True)
    drop_stamps = set(stamps[KEEP_RUNS:])
    for ln in rows:
        for st in drop_stamps:
            if st in ln:
                fid = ln.split()[0] if ln.split() else ""
                if fid:
                    subprocess.run(["youdaonote", "delete", fid], capture_output=True, timeout=120)
                break
    print(f"  云端保留最近 {KEEP_RUNS} 轮（删了 {len(drop_stamps)} 轮旧分片）")


def verify_file(f: str) -> int:
    p = Path(f)
    if not p.exists():
        print(f"  ❌ 不存在：{p}")
        return 1
    try:
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            names = z.namelist()
        print(f"  {'✅' if bad is None else '❌'} zip 可解：{len(names)} 个文件" + ("" if bad is None else f"，损坏：{bad}"))
        need = ["AGENTS.md", "RESTORE.md", "MANIFEST.json"]
        for n in need:
            print(f"  {'✅' if n in names else '❌'} 含 {n}")
        return 0 if bad is None else 1
    except zipfile.BadZipFile as e:
        print(f"  ❌ 不是合法 zip：{e}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--base64", action="store_true")
    ap.add_argument("--verify", default="")
    a = ap.parse_args()

    if a.verify:
        return verify_file(a.verify)

    z = build_zip()
    mirror(z)
    prune_local()
    if not a.local_only:
        if a.base64:
            upload_cloud(z)
        else:
            upload_plaintext()
    print("  完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
