#!/usr/bin/env python3
"""R2 对象存储封装：上传成品、签发预签名下载 URL、删除对象。

设计约束：
  - 只处理成品大文件（pdf/docx/epub），html 预览与图片仍走本地。
  - 任何失败都不抛给调用方，只记录日志并返回 None/False，保证主流程不受影响。
"""
import json
import logging
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("r2")

_CRED_FILE = Path("/root/.translate-r2-cred")

# 只有这三类走 R2
R2_EXTS = {".pdf", ".docx", ".epub"}
R2_PREFIX = "books"
PRESIGN_TTL = 900  # 秒

# ⚠️ 本机出网只有 ~1Mbps（≈110KB/s），必须用「单分片串行」上传：
#   boto3 默认 max_concurrency=10 —— 10 个分片抢 110KB/s，每个分片只分到 ~10KB/s，
#   8MB 分片要 800s，远超 read_timeout → 服务端直接切断连接（"Connection was closed
#   before we received a valid response"），大文件（>multipart_threshold）必然失败。
# 实测：53.5MB / 42.5MB 两个大文件在默认并发下失败，小文件（单请求）全部成功。
# 因此固定 max_concurrency=1 + 放大 read_timeout；单分片 8MB 仅需 ~78s。
MULTIPART_THRESHOLD = 8 * 1024 * 1024
MULTIPART_CHUNKSIZE = 8 * 1024 * 1024
_UPLOAD_CFG = TransferConfig(
    multipart_threshold=MULTIPART_THRESHOLD,
    multipart_chunksize=MULTIPART_CHUNKSIZE,
    max_concurrency=1,
    use_threads=False,
)


def _load_cred() -> dict:
    cfg = {}
    if not _CRED_FILE.exists():
        return cfg
    try:
        for line in _CRED_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    except Exception as e:
        log.warning("读取 R2 凭证失败: %s", e)
    return cfg


def _client(fast: bool = False):
    """fast=True 用于「元数据查询」（列对象/head），超时短、不重试 —— 避免拖慢 web 接口。"""
    c = _load_cred()
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if any(not c.get(k) for k in need):
        return None, None
    try:
        if fast:
            cfg = Config(signature_version="s3v4", retries={"max_attempts": 1},
                         connect_timeout=5, read_timeout=15)
        else:
            cfg = Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=30,
                read_timeout=600,
            )
        s3 = boto3.client(
            "s3",
            endpoint_url="https://%s.r2.cloudflarestorage.com" % c["R2_ACCOUNT_ID"],
            aws_access_key_id=c["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=c["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=cfg,
        )
        return s3, c["R2_BUCKET"]
    except Exception as e:
        log.warning("构造 R2 client 失败: %s", e)
        return None, None


def enabled() -> bool:
    s3, bucket = _client()
    return s3 is not None and bool(bucket)


def object_key(book: str, rel_path: str) -> str:
    return "%s/%s/%s" % (R2_PREFIX, book, rel_path)


def upload(book: str, rel_path: str, local: Path) -> bool:
    """幂等上传：远端已有同尺寸对象则跳过。"""
    s3, bucket = _client()
    if s3 is None:
        return False
    key = object_key(book, rel_path)
    try:
        size = local.stat().st_size
    except OSError:
        return False
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head.get("ContentLength") == size:
            return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchKey", "NotFound"):
            log.warning("head_object 异常 %s: %s", key, e)
    except Exception as e:
        log.warning("head_object 异常 %s: %s", key, e)
    try:
        s3.upload_file(str(local), bucket, key, Config=_UPLOAD_CFG)
        return True
    except Exception as e:
        log.warning("上传失败 %s: %s", key, e)
        return False


def presign(book: str, rel_path: str, filename: str, expires: int = PRESIGN_TTL):
    """签发预签名下载 URL；失败返回 None。"""
    s3, bucket = _client()
    if s3 is None:
        return None
    key = object_key(book, rel_path)
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                # 让浏览器以原名另存，而不是在标签页内联打开
                "ResponseContentDisposition": 'attachment; filename="%s"' % filename,
            },
            ExpiresIn=expires,
        )
    except Exception as e:
        log.warning("签发 URL 失败 %s: %s", key, e)
        return None


def delete_book(book: str) -> int:
    """删除一本书在 R2 上的全部对象（清空回收站时调用）。返回删除数量。"""
    s3, bucket = _client()
    if s3 is None:
        return 0
    prefix = "%s/%s/" % (R2_PREFIX, book)
    n = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                n += len(keys)
    except Exception as e:
        log.warning("删除 %s 的 R2 对象失败: %s", book, e)
    return n


# ===================== 存储快照：web 上展示「本地 vs R2」分工 =====================
SNAP_TTL = 30            # 秒；书库页 4s 轮询时不会每次都打 R2
_snap = {"ts": 0.0, "key": None, "data": None}


def book_objects(book: str):
    """列某本书在 R2 上的对象 {相对路径: 字节数}；未配置/失败返回 None。"""
    s3, bucket = _client(fast=True)
    if s3 is None:
        return None
    prefix = "%s/%s/" % (R2_PREFIX, book)
    out = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out[o["Key"][len(prefix):]] = o["Size"]
    except Exception as e:
        log.warning("列远端对象失败 %s: %s", prefix, e)
        return None
    return out


def snapshot(local_map: dict) -> dict:
    """对比「本地成品」与「R2 对象」。

    local_map: {书名: {相对路径: 本地字节数}}（只放最终成品，如 book.pdf）
    返回 {configured, available, books{...}, totals{...}, cached, age_s}
      books[书名] = {object_count, r2_bytes, local_bytes, expected, missing[], mismatch[], extra[], synced}
    30 秒内且本地尺寸未变则复用缓存（对书库页轮询友好）；R2 不可用时 available=False。
    只打一次 R2（列举 books/ 前缀后按书名分组），避免每本书一次 list。
    """
    import time as _t
    key = json.dumps(local_map, sort_keys=True)
    now = _t.time()
    if _snap["key"] == key and _snap["data"] is not None and now - _snap["ts"] < SNAP_TTL:
        d = dict(_snap["data"])
        d["cached"] = True
        d["age_s"] = int(now - _snap["ts"])
        return d

    cfg = _load_cred()
    configured = all(cfg.get(k) for k in
                     ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))
    view = {"configured": configured, "available": False, "books": {},
            "totals": {"local_files": 0, "local_bytes": 0, "r2_objects": 0, "r2_bytes": 0, "diff_items": 0},
            "cached": False, "age_s": 0}

    # ---- 一次性拉取全部对象，按 books/{书名}/ 分组 ----
    remote_by_book = {}
    if configured:
        s3, bucket = _client(fast=True)
        if s3 is not None:
            try:
                prefix = R2_PREFIX + "/"
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for o in page.get("Contents", []):
                        rest = o["Key"][len(prefix):]
                        book, _, rel = rest.partition("/")
                        if rel:
                            remote_by_book.setdefault(book, {})[rel] = o["Size"]
                view["available"] = True
            except Exception as e:
                log.warning("列举 R2 对象失败: %s", e)

    for book, files in local_map.items():
        local_bytes = sum(files.values())
        view["totals"]["local_files"] += len(files)
        view["totals"]["local_bytes"] += local_bytes
        if not view["available"]:
            continue                      # R2 读不到：该书留空，UI 显示 “?”
        remote = remote_by_book.get(book, {})
        missing = sorted(k for k in files if k not in remote)
        mismatch = sorted(k for k in files if k in remote and remote[k] != files[k])
        extra = sorted(k for k in remote if k not in files)
        view["books"][book] = {
            "object_count": len(remote), "r2_bytes": sum(remote.values()), "local_bytes": local_bytes,
            "expected": len(files), "missing": missing, "mismatch": mismatch, "extra": extra,
            "synced": not missing and not mismatch and len(remote) == len(files),
        }
        view["totals"]["r2_objects"] += len(remote)
        view["totals"]["r2_bytes"] += sum(remote.values())
        view["totals"]["diff_items"] += len(missing) + len(mismatch) + len(extra)
    if configured and view["available"]:
        _snap.update({"ts": now, "key": key, "data": view})
    return view
