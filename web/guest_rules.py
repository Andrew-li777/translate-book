#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translate-web 游客读权限判定"""

# 游客可读的文件扩展名（file 端点）。内部 json/yml/yaml（任务状态/配置）与
# pdf/docx/epub/zip 等成品（走 download 端点）均不给游客。
_GUEST_FILE_EXTS = {
    ".md", ".txt", ".srt", ".csv",           # 文本内容
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp",  # 图片
    ".html",                                  # 内嵌预览
}

# 游客不可读的精确路径段（jobs 详情/SSE、trash、download 下载）
_GUEST_DENY_PREFIX = (
    "/api/jobs/",      # 任务详情 / stream
    "/api/trash",      # 回收站
    "/api/books/",     # 下面是细分：download 拒绝，其余 books 读端点放行
)

# download 端点：游客一律拒绝（关闭下载权限）
_GUEST_DENY_SUB = ("/download/",)

# 游客可读的读端点（GET）
_GUEST_ALLOW_PREFIX = (
    "/static/",
    "/api/books",
    "/api/jobs",        # 列表（/api/jobs 精确）——详情前缀在 DENY 中优先
    "/api/search",
)


def _is_guest_file_path(path: str) -> bool:
    """/api/books/{name}/file/{rel} 的扩展名白名单判定"""
    # 形如 /api/books/NAME/file/PATH 或含子路径
    if "/file/" not in path:
        return True
    # 取 file 后的相对路径最后一段的扩展名
    rel = path.split("/file/", 1)[1]
    if not rel:
        return False
    # 带 query 的去掉
    rel = rel.split("?", 1)[0]
    # 有扩展名且不在白名单 → 拒绝；无扩展名或目录 → 拒绝（保守）
    dot = rel.rfind(".")
    if dot < 0:
        return False
    ext = rel[dot:].lower()
    return ext in _GUEST_FILE_EXTS


def guest_read_allowed(method: str, path: str) -> bool:
    """游客能否访问 (method, path)。仅 GET/HEAD 会走到这里。"""
    p = path.split("?", 1)[0]

    # 任务详情/回收站：严格拒绝
    if p.startswith(("/api/jobs/", "/api/trash")):
        return False

    # file 端点：扩展名白名单（含 books 前缀）
    if "/file/" in p:
        return _is_guest_file_path(p)

    # books 前缀：download 拒绝，其余（详情/chunk/render/cover/glossary）放行
    if p.startswith("/api/books"):
        if any(sub in p for sub in _GUEST_DENY_SUB):
            return False
        return True

    # 其余读端点
    if p == "/api/jobs":
        return True
    if p.startswith("/api/search"):
        return True
    if p.startswith("/static/"):
        return True
    if p in ("/", "/favicon.ico", "/robots.txt", "/sitemap.xml"):
        return True
    return False
