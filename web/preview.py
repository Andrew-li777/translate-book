#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览资源 R2 分发（P2-2）：book.html + images/** 走 R2 公共域，双源过渡。

- 上传由 `preview_sync.py` 完成（独立桶 translate-preview，归档 GC 零改动）
- 本模块只负责「读状态 + URL 重写」：
    ready(name)     该书是否「已完整同步 且 内容未再变动」（.preview_sync_state.json，30s 缓存）
    rewrite(html)   把 /api/books/{name}/file/images/... 换成 {base}/books/{name}/images/...
    summary()       存储页展示用（开关 / 公共域 / 已同步书数）
- 回滚：settings.preview_r2=False 即整体回退本地直连（旗标，无需改代码）
- 安全：仅重写 images/ 路径（与同步范围一致）；书未同步/已变动 → 不重写（自动回退本地）
"""
import json
import logging
import time
from pathlib import Path

from config import WORK_ROOT

log = logging.getLogger("preview")

STATE_FP = WORK_ROOT / ".preview_sync_state.json"
_URL_MARK = "/api/books/%s/file/images/"
_STATE_TTL = 30
_state = {"ts": 0.0, "data": {}}


def invalidate() -> None:
    _state.update({"ts": 0.0, "data": {}})


def _book_sig(name: str) -> float:
    """书当前预览内容签名：book.html + images/* 的最大 mtime（0=无内容）。"""
    d = WORK_ROOT / f"{name}_temp"
    sig = 0.0
    try:
        fp = d / "book.html"
        if fp.is_file():
            sig = max(sig, fp.stat().st_mtime)
        imgs = d / "images"
        if imgs.is_dir():
            for p in imgs.iterdir():
                if p.is_file():
                    sig = max(sig, p.stat().st_mtime)
    except OSError:
        pass
    return sig


def _state_data() -> dict:
    now = time.time()
    if now - _state["ts"] < _STATE_TTL and _state["data"]:
        return _state["data"]
    d = {}
    try:
        raw = json.loads(STATE_FP.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d = raw.get("books") or {}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("读取预览同步状态失败: %s", e)
    _state.update({"ts": now, "data": d})
    return d


def ready(name: str) -> bool:
    """已完整同步 且 内容签名未变（变了=等下次同步，先回退本地）。"""
    rec = _state_data().get(name) or {}
    if not rec.get("complete"):
        return False
    return rec.get("sig") == _book_sig(name)


def base() -> str:
    """当前生效的公共域（开关关 / 未配置 → 空串）。"""
    try:
        import settings
        s = settings.load()
        if not s.get("preview_r2"):
            return ""
        return str(s.get("preview_base") or "").rstrip("/")
    except Exception:
        return ""


def rewrite(html: str, name: str) -> str:
    b = base()
    if not b or not html or not ready(name):
        return html
    return html.replace(_URL_MARK % name, "%s/books/%s/images/" % (b, name))


def summary() -> dict:
    try:
        import settings
        s = settings.load()
    except Exception:
        s = {}
    d = _state_data()
    return {
        "flag": bool(s.get("preview_r2")),
        "base": str(s.get("preview_base") or "").rstrip("/"),
        "synced_books": sum(1 for v in d.values() if v.get("complete")),
    }
