#!/usr/bin/env python3
"""书库扫描与状态判定：读取 /root/translate/work/ 下所有 <书名>_temp/ 目录"""
import json
import re
import time
from pathlib import Path

from config import WORK_ROOT

_TEMP_SUFFIX = "_temp"


def _read_config(dir_path: Path) -> dict:
    """解析 config.txt（键=值）"""
    cfg = {}
    fp = dir_path / "config.txt"
    if fp.exists():
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
        except Exception:
            pass
    return cfg


def _read_meta(dir_path: Path) -> dict:
    meta = {"title": "", "author": "", "output_lang": "zh", "input_lang": "auto"}
    cfg = _read_config(dir_path)
    meta["title"] = cfg.get("original_title", "")
    meta["author"] = cfg.get("creator", "")
    meta["output_lang"] = cfg.get("output_lang", "zh")
    meta["input_lang"] = cfg.get("input_lang", "auto")
    return meta


def _count(dir_path: Path, pattern: str) -> int:
    return len(list(dir_path.glob(pattern)))


def _status(dir_path: Path, manifest: dict | None, chunk_count: int) -> str:
    """依据目录内容判定状态"""
    has_final = any((dir_path / f"book.{ext}").exists() for ext in ("pdf", "docx", "epub"))
    out_count = _count(dir_path, "output_chunk*.md")
    if has_final and (out_count >= chunk_count > 0):
        return "done"
    if chunk_count > 0:
        if out_count > 0:
            return f"translating:{out_count}/{chunk_count}"
        return "converted"
    if (dir_path / "input.md").exists() or (dir_path / "input.html").exists():
        return "converting"
    return "empty"


def scan_books() -> list[dict]:
    """扫描工作区，返回书列表（按 mtime 倒序）"""
    books = []
    if not WORK_ROOT.is_dir():
        return books
    for d in sorted(WORK_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir() or not d.name.endswith(_TEMP_SUFFIX):
            continue
        manifest = {}
        mf = d / "manifest.json"
        if mf.exists():
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        chunk_count = manifest.get("chunk_count", _count(d, "chunk[0-9]*.md"))
        meta = _read_meta(d)
        status = _status(d, manifest, chunk_count)
        books.append({
            "name": d.name[:-len(_TEMP_SUFFIX)],
            "dir": d.name,
            "title": meta["title"] or d.name[:-len(_TEMP_SUFFIX)],
            "author": meta["author"],
            "output_lang": meta["output_lang"],
            "input_lang": meta["input_lang"],
            "status": status,
            "chunk_count": chunk_count,
            "mtime": int(d.stat().st_mtime),
            "files": _file_summary(d),
        })
    return books


def _file_summary(dir_path: Path) -> dict:
    """成品/重要文件清单与大小"""
    out = {}
    for ext, label in (("pdf", "PDF"), ("docx", "DOCX"), ("epub", "EPUB"), ("html", "HTML")):
        fp = dir_path / f"book.{ext}"
        if fp.exists():
            out[f"book.{ext}"] = fp.stat().st_size
    for name in ("output.md", "input.md", "subtitles.srt"):
        fp = dir_path / name
        if fp.exists():
            out[name] = fp.stat().st_size
    return out


def get_book(name: str) -> dict | None:
    """单书详情（含文件清单 + 逐块信息）"""
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", name):
        return None
    d = (WORK_ROOT / f"{name}{_TEMP_SUFFIX}")
    if not d.is_dir():
        return None
    manifest = {}
    mf = d / "manifest.json"
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    chunk_count = manifest.get("chunk_count", _count(d, "chunk[0-9]*.md"))
    chunks = []
    for c in sorted(d.glob("chunk[0-9]*.md")):
        cid = c.stem  # chunk0001
        out = d / f"output_{cid}.md"
        meta = d / f"output_{cid}.meta.json"
        chunks.append({
            "id": cid,
            "src_bytes": c.stat().st_size,
            "translated": out.exists(),
            "trans_bytes": out.stat().st_size if out.exists() else 0,
            "has_meta": meta.exists(),
        })
    return {
        "name": name,
        "dir": d.name,
        "meta": _read_meta(d),
        "status": _status(d, manifest, chunk_count),
        "chunk_count": chunk_count,
        "chunks": chunks,
        "manifest": manifest,
        "files": _file_summary(d),
        "mtime": int(d.stat().st_mtime),
        "images": len(list((d / "images").glob("*"))) if (d / "images").is_dir() else 0,
    }


def resolve_file(name: str, rel_path: str) -> Path | None:
    """安全解析文件路径（防穿越）"""
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", name):
        return None
    base = (WORK_ROOT / f"{name}{_TEMP_SUFFIX}").resolve()
    fp = (base / rel_path).resolve()
    if not (str(fp).startswith(str(base) + "/") or fp == base):
        return None
    if fp.is_file():
        return fp
    return None


def ensure_work_root() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
