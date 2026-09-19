#!/usr/bin/env python3
"""md→HTML 渲染缓存（P0-2）：同一文件（路径+mtime+size）只渲染一次。

- 命中直接返回；文件被管线重写后 mtime/size 变化 → 自动失效重建。
- 字节预算 + 条数上限双限（LRU 淘汰）；进程内缓存（uvicorn 单进程）。
"""
import threading
from collections import OrderedDict
from pathlib import Path

_MAX_ENTRIES = 48
_MAX_BYTES = 64 * 1024 * 1024      # 64MB：最大单文件渲染结果 ~2-3MB，留足余量
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_bytes = 0
_lock = threading.Lock()


def render(name: str, fp: Path, build) -> str:
    """缓存包装：build() 只在缓存未命中时调用。"""
    global _bytes
    try:
        st = fp.stat()
        key = (name, str(fp), st.st_mtime_ns, st.st_size)
    except OSError:
        return build()
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    html = build()
    n = len(html)
    with _lock:
        old = _cache.pop(key, None)
        if old is not None:
            _bytes -= len(old)
        _cache[key] = html
        _bytes += n
        while _cache and (_bytes > _MAX_BYTES or len(_cache) > _MAX_ENTRIES):
            _k, _v = _cache.popitem(last=False)
            _bytes -= len(_v)
    return html


def stats() -> dict:
    with _lock:
        return {"entries": len(_cache), "bytes": _bytes}
