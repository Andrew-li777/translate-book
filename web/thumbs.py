#!/usr/bin/env python3
"""插图缩略图（P1b）：最长边 1600px 的 JPEG，按需生成、落盘复用。

设计：
  - 缓存目录 WORK_ROOT/.thumbs/<book_dir>/<rel>.jpg（在书目录之外，不污染书库扫描/R2 同步）
  - 原图本身就不超过 MAX_SIDE → 直接供原图（不重编码：文字页/小图 JPEG 反而更大更糊）
  - 生成后比原图小不够多（≥90%）→ 弃用缩略图，改供原图（防「压了个寂寞」）
  - 原图 mtime 更新（重译重建）→ 自动重新生成
  - 并发闸门（2）：避免一次开图页把 CPU 打满（服务器 2 核，可能正跑 scan）
  - 任何失败返回 None（调用方降级回原图）
"""
import logging
import os
import threading
from pathlib import Path

from config import WORK_ROOT

log = logging.getLogger("thumbs")

THUMB_ROOT = WORK_ROOT / ".thumbs"
MAX_SIDE = 1600          # 最长边像素
QUALITY = 82             # JPEG 质量
KEEP_RATIO = 0.9         # 缩略图至少比原图小 10% 才值得用
_SEM = threading.Semaphore(2)


def thumb_for(name: str, rel: str, src: Path) -> Path | None:
    """返回缩略图路径（必要时生成）。返回 None = 调用方应直接供原图。"""
    dst = (THUMB_ROOT / f"{name}_temp" / rel).with_suffix(".jpg")
    try:
        s_st = src.stat()
    except OSError:
        return None

    # 已有且比原图新 → 复用（热路径只付两次 stat）
    if dst.exists():
        try:
            d_st = dst.stat()
            if d_st.st_size > 0 and d_st.st_mtime >= s_st.st_mtime:
                return dst
        except OSError:
            pass

    try:
        from PIL import Image, ImageOps
    except Exception as e:                        # Pillow 未装：优雅降级
        log.warning("Pillow 不可用，无法生成缩略图: %s", e)
        return None

    # 原图尺寸探测（失败 → 直接供原图，如 svg）
    try:
        with Image.open(src) as probe:
            dims = probe.size
    except Exception as e:
        log.warning("图片解析失败（直接供原图）%s/%s: %s", name, rel, e)
        return None
    if max(dims) <= MAX_SIDE:
        return None                               # 本来就够小：原图更快更清晰

    tmp = dst.parent / (dst.name + ".tmp")
    try:
        with _SEM:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im0:
                im = ImageOps.exif_transpose(im0)
                im.thumbnail((MAX_SIDE, MAX_SIDE), getattr(getattr(Image, "Resampling", Image), "LANCZOS"))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.save(tmp, "JPEG", quality=QUALITY, optimize=True)
            if tmp.stat().st_size >= int(s_st.st_size * KEEP_RATIO):
                tmp.unlink()                      # 压不小：弃用缩略图，改供原图
                return None
            os.replace(tmp, dst)
        return dst
    except Exception as e:
        log.warning("生成缩略图失败 %s/%s: %s", name, rel, e)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return None
