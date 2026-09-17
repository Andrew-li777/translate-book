#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片引用修复：对齐源 chunk 与 output chunk 的图片引用（补缺失、删多余）。

merge_and_build.py 会做 image validation，output 若比源少图片引用（模型翻译时丢失）
会失败；若比源多图片引用（模型幻觉生成的图，如 `[]![Image 48](images/xxxx.jpg)`）
也会失败。本脚本做确定性修复：把 output 里缺失的图片引用补回、多余的图片引用删除。

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


def remove_extra_imgs(text: str, keep_paths: set) -> tuple[str, int]:
    """删除 output 里源 chunk 不存在的图片引用行（模型幻觉生成的图）。返回(新文本, 删除数)。"""
    lines = text.splitlines()
    out = []
    removed = 0
    for line in lines:
        # 该行包含图片引用且所有引用路径都在 keep 集合外 → 删整行
        paths = [m.group(1) for m in IMG_BLOCK_RE.finditer(line)]
        paths += [m.group(2) for m in IMG_SRC_RE.finditer(line)]
        if paths and all(p not in keep_paths for p in paths):
            removed += 1
            continue
        out.append(line)
    return "\n".join(out), removed


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
    # 先删多余图（源不存在的幻觉图）
    extra_paths = [p for p in out_imgs if p not in src_imgs]
    removed = 0
    if extra_paths:
        out_text, removed = remove_extra_imgs(out_text, set(src_imgs.keys()))
    # 再补缺失图
    added = 0
    for path in missing:
        line = src_imgs[path][0]  # 用源里该图第一处原样
        out_text = out_text.rstrip() + "\n\n" + line + "\n"
        added += 1
    if not missing and not extra_paths:
        return 0
    out.write_text(out_text, encoding="utf-8")
    print(f"补回 {added} 个缺失图片引用: {missing}; 删除 {removed} 个多余图片引用: {extra_paths}")
    return added + removed


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python3 fix_images.py <temp_dir> <chunk_id>")
        sys.exit(2)
    r = fix(sys.argv[1], sys.argv[2])
    sys.exit(0 if r == 0 else 1)