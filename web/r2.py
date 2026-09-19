#!/usr/bin/env python3
"""R2 对象存储封装：上传成品、签发预签名下载 URL、删除对象。

设计约束：
  - 只处理成品大文件（pdf/docx/epub），html 预览与图片仍走本地。
  - 任何失败都不抛给调用方，只记录日志并返回 None/False，保证主流程不受影响。
"""
import logging
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("r2")

_CRED_FILE = Path("/root/.translate-r2-cred")

# 只有这三类走 R2
R2_EXTS = {".pdf", ".docx", ".epub"}
R2_PREFIX = "books"
PRESIGN_TTL = 900  # 秒


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


def _client():
    c = _load_cred()
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if any(not c.get(k) for k in need):
        return None, None
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url="https://%s.r2.cloudflarestorage.com" % c["R2_ACCOUNT_ID"],
            aws_access_key_id=c["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=c["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
            ),
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
        s3.upload_file(str(local), bucket, key)
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
