#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览资源同步（P2-2）：把每本书的 book.html + images/** 上传到独立 R2 桶。

设计约束（对照 R2 实施手册）：
  - **独立桶**（默认 translate-preview）：归档桶保持私有、不绑域；归档 GC 以本地成品
    为基准且只对归档桶生效——预览对象全在新桶，**GC 零改动、零误删风险**
  - **幂等**：ETag(md5) + 尺寸双比对，一致即跳过；重跑安全
  - **完成口径**：一本书全部对象确认在桶后写 `.preview_sync_state.json`
    （complete=true + 内容签名 sig）→ web 端才把该书图片 URL 切到公共域；
    有 pending 时**先写 complete=false**（防裂图），传完再升 true
  - **book.html 加工**：上传前注入 loading="lazy"（与 web 端点同规则）——
    否则 iframe 直连 R2 会一次性拉全量图片
  - 1Mbps 上行：复用 r2.py 的单分片串行上传配置（max_concurrency=1）

用法：
  python3 preview_sync.py --dry-run             # 只清点本地（不连 R2）
  python3 preview_sync.py                       # 全量增量同步（后台跑，约 15-20 分钟）
  python3 preview_sync.py --book 书名            # 只同步一本
  python3 preview_sync.py --prune [--dry-run]   # 【手工】清理远端孤儿（本地已不存在的书/文件）
"""
import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import r2                      # noqa: E402（复用凭据读取与上传配置）
from config import WORK_ROOT   # noqa: E402

STATE_FP = WORK_ROOT / ".preview_sync_state.json"
DEFAULT_BUCKET = "translate-preview"
_LAZY_IMG_RE = re.compile(r"<img(?![^>]*\bloading=)", re.I)


def _cred() -> tuple:
    c = r2._load_cred()
    acc = c.get("R2_PREVIEW_ACCOUNT_ID") or c.get("R2_ACCOUNT_ID")
    kid = c.get("R2_PREVIEW_ACCESS_KEY_ID") or c.get("R2_ACCESS_KEY_ID")
    sec = c.get("R2_PREVIEW_SECRET_ACCESS_KEY") or c.get("R2_SECRET_ACCESS_KEY")
    bucket = c.get("R2_PREVIEW_BUCKET") or DEFAULT_BUCKET
    return acc, kid, sec, bucket


def _client():
    import boto3
    from botocore.config import Config
    acc, kid, sec, _ = _cred()
    if not (acc and kid and sec):
        return None
    return boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % acc,
        aws_access_key_id=kid, aws_secret_access_key=sec, region_name="auto",
        config=Config(signature_version="s3v4",
                      retries={"max_attempts": 3, "mode": "standard"},
                      connect_timeout=30, read_timeout=600),
    )


def _md5_fp(fp: Path) -> str:
    h = hashlib.md5()
    with fp.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _head_match(s3, bucket: str, key: str, fp: Path) -> bool:
    try:
        h = s3.head_object(Bucket=bucket, Key=key)
    except Exception:
        return False
    etag = str(h.get("ETag") or "").strip('"')
    return h.get("ContentLength") == fp.stat().st_size and etag == _md5_fp(fp)


def _book_dir(name: str) -> Path:
    return WORK_ROOT / f"{name}_temp"


def _sig(book_dir: Path) -> float:
    sig = 0.0
    fp = book_dir / "book.html"
    if fp.is_file():
        sig = max(sig, fp.stat().st_mtime)
    imgs = book_dir / "images"
    if imgs.is_dir():
        for p in imgs.iterdir():
            if p.is_file():
                sig = max(sig, p.stat().st_mtime)
    return sig


def _collect_local(book_dir: Path) -> list:
    out = []
    if (book_dir / "book.html").is_file():
        out.append("book.html")
    imgs = book_dir / "images"
    if imgs.is_dir():
        for p in sorted(imgs.iterdir()):
            if p.is_file():
                out.append("images/%s" % p.name)
    return out


def _prepared(rel: str, book_dir: Path, tmpdir: Path) -> Path:
    """book.html → 注入 lazy 的临时副本；其余 → 原文件。"""
    src = book_dir / rel
    if rel == "book.html":
        t = src.read_text(encoding="utf-8", errors="replace")
        t = _LAZY_IMG_RE.sub('<img loading="lazy"', t)
        out = tmpdir / "book.html"
        out.write_text(t, encoding="utf-8")
        return out
    return src


def _state_load() -> dict:
    try:
        d = json.loads(STATE_FP.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _state_set(book: str, rec: dict) -> None:
    d = _state_load()
    d.setdefault("books", {})[book] = rec
    d["updated_at"] = time.time()
    tmp = STATE_FP.with_name(STATE_FP.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FP)


def sync_book(s3, bucket: str, name: str, tmpdir: Path) -> bool:
    book_dir = _book_dir(name)
    rels = _collect_local(book_dir)
    if not rels:
        print("  [skip] %s：无 book.html/images" % name)
        return True
    pending = []
    for rel in rels:
        fp = _prepared(rel, book_dir, tmpdir)
        key = "books/%s/%s" % (name, rel)
        if not _head_match(s3, bucket, key, fp):
            pending.append((rel, fp, key))
    if pending:
        _state_set(name, {"complete": False, "pending": len(pending), "updated_at": time.time()})
        total = len(pending)
        for i, (rel, fp, key) in enumerate(pending, 1):
            t0 = time.time()
            s3.upload_file(str(fp), bucket, key,
                           ExtraArgs={"ContentType": r2.content_type_for(rel)},
                           Config=r2._UPLOAD_CFG)
            print("  [%d/%d] %s  %.0fKB  %.1fs" % (i, total, rel, fp.stat().st_size / 1024, time.time() - t0))
        bad = [k for _, fp, k in pending if not _head_match(s3, bucket, k, fp)]
        if bad:
            print("  [FAIL] %s：%d 个对象复核不符，保持未完成" % (name, len(bad)))
            return False
    _state_set(name, {"complete": True, "files": len(rels), "sig": _sig(book_dir),
                      "synced_at": time.time()})
    print("  [ok] %s：%d 个对象已在桶" % (name, len(rels)))
    return True


def _prune(s3, bucket: str, local_books: list, dry: bool) -> int:
    local_set = set(local_books)
    local_files = {n: set(_collect_local(_book_dir(n))) for n in local_books}
    to_del = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix="books/"):
            for o in page.get("Contents", []):
                rest = o["Key"][len("books/"):]
                book, _, rel = rest.partition("/")
                if not rel:
                    continue
                if book not in local_set or rel not in local_files.get(book, set()):
                    to_del.append(o["Key"])
    except Exception as e:
        print("prune 列举失败: %s" % str(e)[:200])
        return 4
    if not to_del:
        print("prune: 无孤儿")
        return 0
    for k in to_del:
        print("  %s %s" % ("[dry]" if dry else "删除", k))
    if dry:
        print("prune(dry): 共 %d 个待删对象" % len(to_del))
        return 0
    for i in range(0, len(to_del), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in to_del[i:i + 1000]]})
    print("prune: 已删除 %d 个对象" % len(to_del))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--book")
    ap.add_argument("--prune", action="store_true")
    a = ap.parse_args()

    acc, kid, sec, bucket = _cred()

    books_found = []
    for d in sorted(WORK_ROOT.iterdir()):
        if d.is_dir() and d.name.endswith("_temp") and \
                ((d / "book.html").is_file() or (d / "images").is_dir()):
            books_found.append(d.name[:-len("_temp")])
    if a.book:
        books_found = [b for b in books_found if b == a.book]
    books_found.sort()

    if a.dry_run and not a.prune:
        n_files = n_bytes = 0
        print("bucket=%s（dry-run 不连 R2）" % bucket)
        for name in books_found:
            d = _book_dir(name)
            rels = _collect_local(d)
            sz = sum((d / rel).stat().st_size for rel in rels)
            n_files += len(rels)
            n_bytes += sz
            print("  %-52s %4d 个对象 %8.1f MB" % (name, len(rels), sz / 1024 / 1024))
        print("合计：%d 本书 / %d 个对象 / %.1f MB（1Mbps 上行全量首传约 %.0f 分钟）"
              % (len(books_found), n_files, n_bytes / 1024 / 1024, n_bytes * 8 / 1e6 / 60))
        print("启用开关：python3 preview_sync.py（需桶已建 + token 已授权）")
        return 0

    if not (acc and kid and sec):
        print("FAIL: R2 凭证不完整")
        return 1
    s3 = _client()
    if s3 is None:
        print("FAIL: 构造 S3 客户端失败")
        return 1
    try:
        s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as e:
        print("FAIL: 访问桶 %s 失败（桶不存在 / token 未授权该桶？）：%s" % (bucket, str(e)[:220]))
        return 2

    if a.prune:
        return _prune(s3, bucket, books_found, dry=a.dry_run)

    ok_all = True
    with tempfile.TemporaryDirectory(prefix="preview_sync_") as td:
        tmpdir = Path(td)
        for name in books_found:
            t0 = time.time()
            print("同步《%s》…" % name)
            ok = sync_book(s3, bucket, name, tmpdir)
            ok_all = ok_all and ok
            print("  用时 %.1fs" % (time.time() - t0))
    print("完成：%d 本书（bucket=%s）" % (len(books_found), bucket))
    return 0 if ok_all else 3


if __name__ == "__main__":
    sys.exit(main())
