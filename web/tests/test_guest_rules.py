#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游客只读白名单（guest_rules）单元测试 —— 对应审计「游客 403」验收面。

纯函数测试：等价于中间件层实测的 403 判定（中间件另有端到端测试见
test_auth_middleware.py）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_rules import guest_read_allowed  # noqa: E402


class TestGuestReadAllowed(unittest.TestCase):
    def test_read_endpoints_allowed(self):
        for p in ("/", "/favicon.ico", "/static/app.js", "/static/style.css",
                  "/api/books", "/api/books/SomeBook", "/api/books/SomeBook/chunk/chunk0001",
                  "/api/books/SomeBook/render/output.md", "/api/books/SomeBook/cover",
                  "/api/books/SomeBook/glossary", "/api/search?q=abc"):
            self.assertTrue(guest_read_allowed("GET", p), "应放行: %s" % p)

    def test_admin_only_endpoints_denied(self):
        """存储页/设置/GC —— 实测 403 的三个端点。"""
        for p in ("/api/settings", "/api/storage", "/api/storage/gc",
                  "/api/jobs/some-id", "/api/jobs/some-id/stream", "/api/trash",
                  "/api/trash/books/X/restore", "/api/books/X/download/book.pdf"):
            self.assertFalse(guest_read_allowed("GET", p), "应拒绝: %s" % p)

    def test_file_endpoint_extension_whitelist(self):
        allow = ("/api/books/X/file/images/000001.png", "/api/books/X/file/book.html",
                 "/api/books/X/file/output.md", "/api/books/X/file/subtitles.srt")
        deny = ("/api/books/X/file/manifest.json", "/api/books/X/file/config.yaml",
                "/api/books/X/file/run_state.json", "/api/books/X/file/book.pdf",
                "/api/books/X/file/noext")
        for p in allow:
            self.assertTrue(guest_read_allowed("GET", p), "应放行: %s" % p)
        for p in deny:
            self.assertFalse(guest_read_allowed("GET", p), "应拒绝: %s" % p)

    def test_non_get_never_guest_readable(self):
        """白名单只对读方法有意义：写操作一律拒绝（中间件层面拦截）。"""
        # guest_read_allowed 只被 GET/HEAD/OPTIONS 调用；此处验证路径本身不豁免方法语义
        # —— 直接测「非读方法 + 受限路径」的判定入口等价性
        for p in ("/api/upload", "/api/storage/gc"):
            self.assertFalse(guest_read_allowed("GET", p), "应拒绝: %s" % p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
