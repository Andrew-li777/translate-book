#!/usr/bin/env python3
"""扫描 work/*_temp，把成品 pdf/docx/epub 幂等同步到 R2。

用法：
    /root/translate/book/.venv/bin/python /root/translate/web/r2_sync.py
    /root/translate/book/.venv/bin/python /root/translate/web/r2_sync.py --dry-run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, "/root/translate/web")

import config  # noqa: E402
import r2      # noqa: E402

TEMP_SUFFIX = "_temp"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not r2.enabled():
        print("R2 未启用（凭证缺失或不可用），退出", file=sys.stderr)
        return 2

    if not config.WORK_ROOT.is_dir():
        print("工作区不存在: %s" % config.WORK_ROOT, file=sys.stderr)
        return 2

    ok = skip = miss = fail = 0
    for d in sorted(config.WORK_ROOT.glob("*%s" % TEMP_SUFFIX)):
        if not d.is_dir():
            continue
        book = d.name[: -len(TEMP_SUFFIX)]
        for ext in ("pdf", "docx", "epub"):
            fp = d / ("book.%s" % ext)
            if not fp.exists():
                continue
            size_mb = fp.stat().st_size / 1024 / 1024
            if args.dry_run:
                print("[dry-run] 待检查 %s/book.%s (%.1f MB)" % (book, ext, size_mb))
                ok += 1
                continue
            if r2.upload(book, "book.%s" % ext, fp):
                print("✓ %s/book.%s (%.1f MB)" % (book, ext, size_mb))
                ok += 1
            else:
                print("✗ %s/book.%s 上传失败" % (book, ext), file=sys.stderr)
                fail += 1

    print("\n完成：成功/已存在 %d，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
