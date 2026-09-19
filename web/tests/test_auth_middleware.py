#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴权中间件端到端测试 —— 对应审计「游客 403」验收面（中间件层）。

不依赖 httpx：用最小 ASGI 调用直达 Starlette 应用。
覆盖：游客放行/403、错误凭据 401（不降级）、管理员放行、限频 429、
校验结果缓存（只缓存成功）、凭据文件 fail-closed。"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcrypt  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import auth  # noqa: E402
from auth import AuthMiddleware, load_admin_cred  # noqa: E402
from guest_rules import guest_read_allowed  # noqa: E402

USER = "translate"
PASSWORD = "pw-123"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


def _basic(user, password):
    _tok = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    return "Basic " + _tok


def _build_app(rate_limits=None):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/{full_path:path}", ok, methods=["GET", "POST", "HEAD", "OPTIONS"])])
    app.add_middleware(
        AuthMiddleware,
        admin_user=USER,
        admin_hash=HASH,
        is_read_allowed=guest_read_allowed,
        rate_limits=rate_limits or [],
    )
    return app


def _call(app, method, path, headers=None):
    async def run():
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("9.9.9.9", 1234), "server": ("testserver", 80),
        }
        events = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            events.append(msg)

        await app(scope, receive, send)
        status = next((e["status"] for e in events if e["type"] == "http.response.start"), None)
        hdrs = {}
        for e in events:
            if e["type"] == "http.response.start":
                hdrs = {k.decode(): v.decode() for k, v in e.get("headers", [])}
        return status, hdrs

    return asyncio.run(run())


class TestMiddleware(unittest.TestCase):
    def setUp(self):
        auth._auth_cache.clear()
        self.app = _build_app()

    def test_guest_read_allowed(self):
        status, _ = _call(self.app, "GET", "/api/books")
        self.assertEqual(status, 200)

    def test_guest_denied_403(self):
        for method, path in (("GET", "/api/storage"), ("GET", "/api/settings"),
                             ("POST", "/api/upload"), ("GET", "/api/jobs/abc")):
            status, hdrs = _call(self.app, method, path)
            self.assertEqual(status, 403, "%s %s 应 403" % (method, path))
            self.assertEqual(hdrs.get("x-guest-only"), "1")

    def test_bad_credentials_401(self):
        for header in (_basic(USER, "wrong-pass"), "Bearer not-basic"):
            status, _ = _call(self.app, "GET", "/api/books", {"authorization": header})
            self.assertEqual(status, 401, "错误凭据必须 401（不降级为游客）")

    def test_admin_allowed(self):
        h = {"authorization": _basic(USER, PASSWORD)}
        self.assertEqual(_call(self.app, "POST", "/api/upload", h)[0], 200)
        self.assertEqual(_call(self.app, "GET", "/api/storage", h)[0], 200)

    def test_rate_limit_429(self):
        app = _build_app(rate_limits=[("/api/search", 2, 60)])
        self.assertEqual(_call(app, "GET", "/api/search?q=a")[0], 200)
        self.assertEqual(_call(app, "GET", "/api/search?q=b")[0], 200)
        self.assertEqual(_call(app, "GET", "/api/search?q=c")[0], 429)


class TestAuthCache(unittest.TestCase):
    def setUp(self):
        auth._auth_cache.clear()

    def test_success_cached(self):
        header = _basic(USER, PASSWORD)
        with mock.patch.object(auth, "_basic_ok", return_value=True) as m:
            self.assertTrue(auth._basic_ok_cached(header, USER, "H"))
            self.assertTrue(auth._basic_ok_cached(header, USER, "H"))
            self.assertEqual(m.call_count, 1, "成功校验应命中缓存（只付一次 bcrypt）")

    def test_failure_not_cached(self):
        header = _basic(USER, "nope")
        with mock.patch.object(auth, "_basic_ok", return_value=False) as m:
            self.assertFalse(auth._basic_ok_cached(header, USER, "H"))
            self.assertFalse(auth._basic_ok_cached(header, USER, "H"))
            self.assertEqual(m.call_count, 2, "失败必须每次走完整校验（保持爆破成本）")

    def test_cred_fingerprint_mismatch(self):
        """凭据指纹（user+hash）变化 → 旧缓存条目不命中。"""
        header = _basic(USER, PASSWORD)
        with mock.patch.object(auth, "_basic_ok", return_value=True) as m:
            self.assertTrue(auth._basic_ok_cached(header, USER, "H1"))
            self.assertTrue(auth._basic_ok_cached(header, USER, "H2"))
            self.assertEqual(m.call_count, 2, "轮换哈希后旧缓存应失配")


class TestLoadAdminCred(unittest.TestCase):
    def test_reads_chinese_and_equals(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "cred"
            lines = [
                "# comment",
                "账号" + ": " + "testuser",
                "bcrypt" + ": " + "$2b$04$abcdefghijklmnopqrstuv",
            ]
            fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            user, h = load_admin_cred(str(fp))
            self.assertEqual(user, "testuser")
            self.assertTrue(h.startswith("$2b$"))

    def test_missing_file_fail_closed(self):
        user, h = load_admin_cred("/tmp/no-such-cred-file-xyz")
        self.assertEqual(h, "", "读不到哈希必须 fail closed（返回空哈希）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
