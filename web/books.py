#!/usr/bin/env python3
"""书库扫描与状态判定：读取 /root/translate/work/ 下所有 <书名>_temp/ 目录"""
import json
import logging
import re
import time
from pathlib import Path

from config import WORK_ROOT
import preview

log = logging.getLogger("books")
_TEMP_SUFFIX = "_temp"
_FINAL_EXTS = ("pdf", "docx", "epub")   # 只有这三类会上 R2


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


def _progress(status: str, chunk_count: int) -> dict:
    """把状态字符串解析成结构化进度，供前端画进度条

    返回 {done, total, pct, active}
      done/total  已译块数 / 总块数
      pct         百分比（0-100，total 未知时给 0）
      active      是否正在处理（translating/converting）→ 前端据此开启自动刷新
    """
    done, total = 0, chunk_count or 0
    if status.startswith("translating:"):
        try:
            a, b = status.split(":", 1)[1].split("/")
            done, total = int(a), int(b)
        except Exception:
            pass
    elif status == "done":
        done = total
    pct = int(round(done * 100 / total)) if total > 0 else 0
    return {
        "done": done,
        "total": total,
        "pct": min(pct, 100),
        "active": status.startswith("translating") or status == "converting",
    }


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
            "progress": _progress(status, chunk_count),
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


# ===================== 存储分工（本地 vs R2）=====================
_EMPTY_STORAGE = {
    "configured": False, "available": False, "books": {},
    "totals": {"local_files": 0, "local_bytes": 0, "r2_objects": 0, "r2_bytes": 0, "diff_items": 0},
    "cached": False, "age_s": 0,
}


def _final_files(files: dict) -> dict:
    """只取「会上 R2 的成品」（book.pdf/docx/epub）"""
    return {k: v for k, v in (files or {}).items()
            if k.startswith("book.") and k.rsplit(".", 1)[-1] in _FINAL_EXTS}


def storage_view(book_list: list) -> dict:
    """本地成品 vs R2 对象对比（r2.snapshot 内含 30s 缓存）。

    R2 未配置 / 不可用 / 任何异常 → 返回空视图（configured/available=False），
    前端据此显示「R2 —」或「R2 ?」，绝不影响书库本身的可用性。
    """
    try:
        import r2
        local_map = {b["name"]: _final_files(b.get("files")) for b in book_list}
        local_map = {k: v for k, v in local_map.items() if v}
        if not local_map:
            return dict(_EMPTY_STORAGE)
        return r2.snapshot(local_map)
    except Exception as e:
        log.warning("存储快照失败: %s", e)
        return dict(_EMPTY_STORAGE)


R2_FREE_QUOTA = 10 * 1024 ** 3      # Cloudflare R2 免费额度 10GB（展示用）
_WORK_ROOT_BYTES = {"ts": 0.0, "n": 0}
_WORK_ROOT_TTL = 60


def _dir_bytes(root: Path) -> int:
    """工作区实际占用（60s 缓存 —— 遍历 400MB 目录不宜每次请求都做）"""
    import time as _t
    now = _t.time()
    if now - _WORK_ROOT_BYTES["ts"] < _WORK_ROOT_TTL and _WORK_ROOT_BYTES["n"]:
        return _WORK_ROOT_BYTES["n"]
    total = 0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    _WORK_ROOT_BYTES.update({"ts": now, "n": total})
    return total


def _disk_info() -> dict:
    import shutil as _sh
    try:
        u = _sh.disk_usage(str(WORK_ROOT))
        return {"total": u.total, "used": u.used, "free": u.free,
                "pct": round(u.used * 100 / u.total, 1) if u.total else 0}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "pct": 0}


# ===================== S6-B: 容量趋势（历史由 r2_alert.py 每日采样写入）=====================
HIST_FP = Path("/root/translate/work/.storage_history.jsonl")
HIST_POINTS = 30          # 趋势图展示最近 N 个采样点


def read_history(limit: int = HIST_POINTS) -> list:
    """读采样历史（JSONL，一行一天）。文件缺失/某行损坏都只影响该行，绝不抛异常。

    每行：{date, trs_bytes, local_files, local_bytes, r2_objects, r2_bytes, presign}
    R2 不可达的当天，r2_* 写 None（**不能写 0**，否则趋势图会出现假跌）。
    """
    rows = []
    try:
        for line in HIST_FP.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("读取容量历史失败: %s", e)
    return rows[-limit:]


def storage_page(refresh: bool = False) -> dict:
    """存储页数据：逐书三列对照 + 孤儿对象 + 容量汇总（管理员端点用）。

    孤儿对象定义（R2 上有、本地不该有）：
      kind="ghost_book"  本地书已不存在（删书/清空回收站后遗留）
      kind="extra"       书还在，但该文件本地已无（如重建过成品、改过名）
    ⚠️ 只读：本函数不做任何删除。
    """
    import r2
    bs = scan_books()
    local_map = {b["name"]: _final_files(b.get("files") or {}) for b in bs}
    local_map = {k: v for k, v in local_map.items() if v}
    if refresh:
        r2._snap.update({"ts": 0.0, "key": None, "data": None})   # 绕过 30s 快照缓存
        r2.objects_all(force=True)                                # 目录列举只强制这一次（snapshot/孤儿扫描共用）
    view = r2.snapshot(local_map)

    # ---- 逐书三列 ----
    rows, tot = [], {"local_files": 0, "local_bytes": 0, "r2_objects": 0, "r2_bytes": 0, "diff_items": 0}
    for b in bs:
        name = b["name"]
        files = _final_files(b.get("files") or {})
        if not files:
            continue                        # 无成品（未完成的书不进存储页）
        d = view["books"].get(name) or {}
        rows.append({
            "name": name, "title": b.get("title") or name, "status": b.get("status"),
            "local_files": len(files), "local_bytes": sum(files.values()),
            "r2_objects": d.get("object_count", 0), "r2_bytes": d.get("r2_bytes", 0),
            "expected": len(files),
            "missing": d.get("missing", []), "mismatch": d.get("mismatch", []),
            "extra": d.get("extra", []),
            "synced": d.get("synced", False),
        })
        tot["local_files"] += len(files)
        tot["local_bytes"] += sum(files.values())
        tot["r2_objects"] += d.get("object_count", 0)
        tot["r2_bytes"] += d.get("r2_bytes", 0)
        tot["diff_items"] += len(d.get("missing", [])) + len(d.get("mismatch", [])) + len(d.get("extra", []))

    # ---- 孤儿对象 ----
    orphans, orphan_bytes = [], 0
    if view["available"]:
        remote = r2.objects_all() or {}   # refresh 时已在上面 force 过一次，这里直接命中同一份
        for book, objs in remote.items():
            if book not in local_map:
                for rel, size in sorted(objs.items()):
                    orphans.append({"key": "%s/%s/%s" % (r2.R2_PREFIX, book, rel),
                                    "book": book, "rel": rel, "size": size,
                                    "kind": "ghost_book"})
                    orphan_bytes += size
            else:
                for rel, size in sorted(objs.items()):
                    if rel not in local_map[book]:
                        orphans.append({"key": "%s/%s/%s" % (r2.R2_PREFIX, book, rel),
                                        "book": book, "rel": rel, "size": size,
                                        "kind": "extra"})
                        orphan_bytes += size

    return {
        "configured": view["configured"], "available": view["available"],
        "cached": view["cached"], "age_s": view["age_s"],
        "rows": rows, "totals": tot,
        "orphans": orphans, "orphan_bytes": orphan_bytes,
        "disk": _disk_info(), "work_bytes": _dir_bytes(WORK_ROOT),
        "quota_bytes": R2_FREE_QUOTA,
        "history": read_history(),
        "stats": _presign_stats(),
        "preview": preview.summary(),
    }


def _presign_stats() -> dict:
    try:
        import settings
        return settings.presign_stats()
    except Exception:
        return {"count": 0, "last": 0}


def storage_gc(keys: list) -> dict:
    """删除**孤儿对象**（管理员操作，双重复核后才删）。

    安全约束：
      1. 每个 key 必须形如 books/{书名}/{相对路径}；
      2. 删前**重新拉一次远端**确认它确实是孤儿 —— 本地存在同名成品的一律拒绝
         （用户可能在 GC 期间刚好补传，绝不能删掉唯一副本）；
      3. 单次上限 500 个。
    """
    import r2
    if not keys:
        return {"deleted": 0, "refused": [], "error": "未指定对象"}
    if len(keys) > 500:
        return {"deleted": 0, "refused": [], "error": "单次最多清理 500 个对象"}

    local_map = {b["name"]: _final_files(b.get("files") or {}) for b in scan_books()}
    local_map = {k: v for k, v in local_map.items() if v}
    remote = r2.objects_all(force=True)
    if remote is None:
        return {"deleted": 0, "refused": [], "error": "R2 读不到，已放弃清理（不冒删错的风险）"}

    refuse, ok = [], []
    for k in keys:
        parts = str(k).split("/")
        if len(parts) < 3 or parts[0] != r2.R2_PREFIX:
            refuse.append({"key": k, "why": "路径不合法"})
            continue
        book, rel = parts[1], "/".join(parts[2:])
        if book in local_map and rel in local_map[book]:
            refuse.append({"key": k, "why": "本地存在同名成品，拒绝删除"})
            continue
        if book not in remote or rel not in remote[book]:
            refuse.append({"key": k, "why": "远端已无此对象"})
            continue
        ok.append(k)

    n = r2.delete_objects(ok) if ok else 0
    _snap_clear()
    return {"deleted": n, "refused": refuse, "files": ok}


def _snap_clear():
    try:
        import r2
        r2._snap.update({"ts": 0.0, "key": None, "data": None})
    except Exception:
        pass


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
        "progress": _progress(_status(d, manifest, chunk_count), chunk_count),
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
