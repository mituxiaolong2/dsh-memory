#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_bm25.py —— 记忆库的 BM25 检索器（把"凭感觉翻文件"换成"算分排序"）

把 ~/.dsh/memory/*.md 和 AGENTS.md 切成"条目块"，建 BM25 索引，按查询打分排序。

用法：
  python memory_bm25.py --rebuild                 重建索引
  python memory_bm25.py -q "表情包 BOM 文件名"    查询（默认 top 5）
  python memory_bm25.py -q "缴费" --top 3 --full  显示完整块
  python memory_bm25.py --stats                   看索引规模

索引缓存在 ~/.dsh/cache/memory-bm25.json；文件改过会自动重建。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

HOME = Path.home()
MEM_DIR = HOME / ".dsh" / "memory"
AGENTS = HOME / ".dsh" / "AGENTS.md"
CACHE = HOME / ".dsh" / "cache" / "memory-bm25.json"
HITLOG = HOME / ".dsh" / "cache" / "memory-hits.jsonl"   # 每次命中的记录 = 真·「最后提及」
CACHE_ALL = HOME / ".dsh" / "cache" / "memory-bm25-all.json"

K1, B = 1.5, 0.75

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import memory_vector as MV
except Exception:
    MV = None

MAX_CHUNK_CHARS = 900

ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]*")
CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中文双字切分 + 英文/数字整词。零依赖，够用。"""
    toks: list[str] = []
    for w in ASCII_WORD.findall(text):
        toks.append(w.lower())
    cjk = "".join(CJK.findall(text))
    for i in range(len(cjk) - 1):
        toks.append(cjk[i:i + 2])
    return toks


def split_chunks(path: Path) -> list[dict]:
    """按空行/标题切块；过长的块再按字符数硬切。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    chunks: list[dict] = []
    buf: list[str] = []
    start = 0

    def flush(end_line: int):
        nonlocal buf, start
        body = "\n".join(buf).strip()
        if body:
            chunks.append({"file": path.name, "line": start + 1, "text": body,
                           "ctx": heading})
        buf = []
        start = end_line

    heading = ""
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("#"):
            flush(i - 1 if buf else i)
            heading = stripped.lstrip("#").strip()
            buf = [ln]
            start = i
            continue
        if not stripped and buf:
            flush(i)
            continue
        if not buf:
            start = i
        buf.append(ln)
    if buf:
        flush(len(lines))

    # 硬切过长块
    out: list[dict] = []
    for c in chunks:
        if len(c["text"]) <= MAX_CHUNK_CHARS:
            out.append(c)
            continue
        parts = []
        cur = ""
        for para in c["text"].split("\n"):
            if len(cur) + len(para) > MAX_CHUNK_CHARS and cur:
                parts.append(cur)
                cur = para
            else:
                cur = f"{cur}\n{para}" if cur else para
        if cur:
            parts.append(cur)
        for idx, p in enumerate(parts):
            out.append({**c, "text": p, "part": idx + 1})
    return out


SKIP_DEFAULT = {"90-遗忘.md", "00-索引.md"}   # 遗忘区不进常规检索；索引是导航层，也不进（避免标题词抢位）
LONG_TERM_PREFIX = ("70-", "71-", "90-", "91-", "92-")  # 长期层：完成区 / 归档区 / 遗忘区
RECALL_LOG = HOME / ".dsh" / "cache" / "memory-recall.jsonl"


def collect_chunks(include_forgotten: bool = False) -> list[dict]:
    files = sorted(p for p in MEM_DIR.glob("*.md")
                   if include_forgotten or p.name not in SKIP_DEFAULT)
    if AGENTS.exists():
        files.append(AGENTS)
    chunks: list[dict] = []
    for f in files:
        chunks.extend(split_chunks(f))
    return chunks


def build_index(include_forgotten: bool = False) -> dict:
    chunks = collect_chunks(include_forgotten)
    docs = []
    df: Counter = Counter()
    for c in chunks:
        toks = tokenize((c.get("ctx") or "") + "\n" + c["text"])
        tf = Counter(toks)
        docs.append({
            "file": c["file"],
            "line": c.get("line", 1),
            "part": c.get("part"),
            "text": c["text"],
            "len": len(toks),
            "tf": dict(tf),
        })
        for t in tf:
            df[t] += 1
    avgdl = sum(d["len"] for d in docs) / len(docs) if docs else 0
    idx = {
        "built": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "n_docs": len(docs),
        "avgdl": avgdl,
        "df": dict(df),
        "docs": docs,
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


def load_index(force: bool = False, include_forgotten: bool = False) -> dict:
    newest = 0.0
    for p in list(MEM_DIR.glob("*.md")) + ([AGENTS] if AGENTS.exists() else []):
        newest = max(newest, p.stat().st_mtime)
    if not force and CACHE.exists() and CACHE.stat().st_mtime >= newest:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return build_index(include_forgotten)


def search(idx: dict, query: str, top: int = 5, hybrid: bool = False) -> list[dict]:
    q_tokens = list(dict.fromkeys(tokenize(query)))
    n = idx["n_docs"] or 1
    avgdl = idx["avgdl"] or 1
    docs = idx["docs"]

    # 1) 纯 BM25 分数（全部块都算，含 0）
    bm = [0.0] * len(docs)
    hits_all = []
    compact = re.sub(r"\s+", "", query).lower()
    for i, d in enumerate(docs):
        score = 0.0
        dl = d["len"] or 1
        for tk in q_tokens:
            tf = d["tf"].get(tk, 0)
            if not tf:
                continue
            df = idx["df"].get(tk, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            if compact and len(compact) >= 3 and compact in d["text"].lower().replace(" ", ""):
                score *= 1.8
            hits = [tk for tk in q_tokens if d["tf"].get(tk)]
            score *= 1 + 0.12 * max(0, len(hits) - 1)
        bm[i] = score
        hits_all.append([tk for tk in q_tokens if d["tf"].get(tk)])

    # 2) 语义混合：BM25 归一 + cosine 归一，全库参与（不只是命中块）
    if hybrid and MV is not None and MV.available():
        try:
            sims = MV.sims(query, docs)
            bm_max = max(bm) or 1.0
            scores = [0.4 * (bm[i] / bm_max) + 0.6 * float((sims[i] + 1) / 2) for i in range(len(docs))]
            mode = "混合"
        except Exception:
            scores, mode = bm, "BM25"
    else:
        scores, mode = bm, "BM25"

    order = sorted(range(len(docs)), key=lambda i: -scores[i])[:top]
    out = []
    for i in order:
        if scores[i] <= 0:
            continue
        d = docs[i]
        out.append({**d, "score": scores[i], "hits": hits_all[i], "mode": mode})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", default="")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--full", action="store_true", help="打印完整块内容")
    ap.add_argument("--all", action="store_true", help="包含 90-遗忘.md")
    ap.add_argument("--hybrid", action="store_true", help="（已默认开启）BM25 + 向量语义混合")
    ap.add_argument("--bm25-only", action="store_true", help="只用 BM25，不加载向量模型")
    ap.add_argument("--recall", action="store_true",
                    help="回忆模式：只用关键词去长期层（70/71/90/91/92）高置信匹配，触发就记日志")
    ap.add_argument("--recall-threshold", type=float, default=3.0, help="回忆触发的最低分（默认 3.0）")
    a = ap.parse_args()

    if a.recall and a.query:
        idx = load_index(force=a.rebuild, include_forgotten=True)
        results = [r for r in search(idx, a.query, max(a.top, 5), hybrid=False)
                   if str(r["file"]).startswith(LONG_TERM_PREFIX)]
        best = results[0] if results else None
        hit = bool(best and float(best["score"]) >= a.recall_threshold)
        import json as _json, time as _time
        RECALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RECALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": int(_time.time()), "q": a.query, "fired": hit,
                "file": best["file"] if best else None,
                "line": best.get("line") if best else None,
                "score": round(float(best["score"]), 3) if best else None,
                "threshold": a.recall_threshold,
            }, ensure_ascii=False) + chr(10))
        if not hit:
            print(f"  回忆未触发（最高分 {float(best['score']):.2f} < {a.recall_threshold}）" if best
                  else "  回忆未触发（长期层没有相关条目）")
            return 1
        print(f"  💭 回忆触发：{best['file']}:{best.get('line')}  分数 {float(best['score']):.2f}")
        print("  " + best["text"][:300].replace("\n", " "))
        return 0

    idx = load_index(force=a.rebuild, include_forgotten=a.all)

    if a.stats or (not a.query and not a.rebuild):
        print(f"  索引: {idx['n_docs']} 块 / 词表 {len(idx['df'])} / 平均长度 {idx['avgdl']:.0f} token")
        print(f"  构建于 {idx['built']}")
        if not a.query:
            return 0

    if a.rebuild and not a.query:
        print("  ✅ 重建完成")
        return 0

    if not a.query:
        return 0

    use_hybrid = (not a.bm25_only)
    results = search(idx, a.query, a.top, hybrid=use_hybrid)

    # 记「谁被引用了」——比人工打日期可靠，遗忘机制靠它判沉睡
    if results:
        import json as _json
        import time as _time
        HITLOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            with HITLOG.open("a", encoding="utf-8") as fh:
                for r in results:
                    fh.write(_json.dumps({
                        "ts": int(_time.time()),
                        "q": a.query,
                        "file": r["file"],
                        "line": r.get("line"),
                        "score": round(float(r["score"]), 3),
                    }, ensure_ascii=False) + chr(10))
        except OSError:
            pass
    if not results:
        print("  没命中（换个说法试试）")
        return 1
    print(f"  查询：{a.query}")
    for i, r in enumerate(results, 1):
        loc = f"{r['file']}:{r['line']}"
        print(f"\n  [{i}] {r['score']:.2f}  {loc}" + (f"  命中词 {','.join(r['hits'][:6])}" if r["hits"] else ""))
        body = r["text"] if a.full else r["text"][:160].replace("\n", " ")
        for ln in body.splitlines():
            print("      " + ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
