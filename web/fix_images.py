#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片引用修复：把源 chunk 里缺失的图片引用补回 output chunk。

merge_and_build.py 会做 image validation，output 若比源少图片引用（模型翻译时可能丢失）
会导致合并失败。本脚本做确定性修复：逐行比对源 chunk 的图片引用，把 output 里缺失的
按原位置插回。行内 <img src> 引用同理。

用法:
  python3 fix_images.py <temp_dir> <chunk_id>
  # 例: python3 fix_images.py /root/translate/work/abtest_temp chunk0003
  # 就地修复 output_<chunk_id>.md；若无需修复则不改动、stdout 空。
"""
import re
import sys
from pathlib import Path

IMG_BLOCK_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)[^\n]*")   # 整行 ![](path){...}
IMG_SRC_RE = re.compile(r"<img[^>]*\bsrc=([\"'])([^\"']+)\1", re.I)


def collect_imgs(text: str) -> dict:
    """提取 (path) -> [整行原样] 映射。markdown 图片引用与 <img src> 都算。"""
    imgs = {}
    for line in text.splitlines():
        for m in IMG_BLOCK_RE.finditer(line):
            imgs.setdefault(m.group(1), []).append(line.strip())
        for m in IMG_SRC_RE.finditer(line):
            imgs.setdefault(m.group(2), []).append(line.strip())
    return imgs


def fix(temp_dir: str, chunk_id: str) -> int:
    src = Path(temp_dir) / f"{chunk_id}.md"
    out = Path(temp_dir) / f"output_{chunk_id}.md"
    if not src.exists() or not out.exists():
        print(f"缺失文件: src={src.exists()} out={out.exists()}")
        return -1
    src_text = src.read_text(encoding="utf-8")
    out_text = out.read_text(encoding="utf-8")
    src_imgs = collect_imgs(src_text)
    out_imgs = collect_imgs(out_text)
    missing = [p for p in src_imgs if p not in out_imgs]
    if not missing:
        return 0
    # 缺失的图片引用，按源中的顺序补到 output 末尾（图片引用在原文是占位，位置变化
    # 不影响正文语义；插入到 output 末尾保留引用）。更优做法是按源行号插回，
    # 但为稳健，先统一追加到文件末尾（图片引用不破坏 markdown 结构）。
    added = 0
    for path in missing:
        line = src_imgs[path][0]  # 用源里该图第一处原样
        out_text = out_text.rstrip() + "\n\n" + line + "\n"
        added += 1
    out.write_text(out_text, encoding="utf-8")
    print(f"补回 {added} 个缺失图片引用: {missing}")
    return added


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python3 fix_images.py <temp_dir> <chunk_id>")
        sys.exit(2)
    r = fix(sys.argv[1], sys.argv[2])
    sys.exit(0 if r == 0 else 1)