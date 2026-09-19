#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染缓存（render_cache）单元测试 —— 对应审计「渲染缓存失效」验收面。

覆盖：命中/构建次数、mtime 变化自动失效、LRU 条数上限、字节上限、文件缺失直通。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render_cache  # noqa: E402


class TestRenderCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = (render_cache._MAX_ENTRIES, render_cache._MAX_BYTES)
        self.addCleanup(self._restore)
        render_cache._cache.clear()
        render_cache._bytes = 0

    def _restore(self):
        render_cache._MAX_ENTRIES, render_cache._MAX_BYTES = self._saved
        render_cache._cache.clear()
        render_cache._bytes = 0

    def _mkfile(self, name="a.md", content="hello"):
        fp = Path(self.tmp.name) / name
        fp.write_text(content, encoding="utf-8")
        return fp

    def test_build_once_then_hit(self):
        fp = self._mkfile()
        calls = []

        def build():
            calls.append(1)
            return "<p>rendered</p>"

        r1 = render_cache.render("BookA", fp, build)
        r2 = render_cache.render("BookA", fp, build)
        self.assertEqual(r1, r2)
        self.assertEqual(len(calls), 1, "第二次应命中缓存、不再 build")

    def test_mtime_change_invalidates(self):
        fp = self._mkfile()
        calls = []
        render_cache.render("BookA", fp, lambda: calls.append(1) or "<p>v1</p>")
        os.utime(fp, ns=(1_700_000_000_111_111_111, 1_700_000_000_111_111_111))
        out = render_cache.render("BookA", fp, lambda: calls.append(1) or "<p>v2</p>")
        self.assertEqual(len(calls), 2, "mtime 变化后应重建")
        self.assertEqual(out, "<p>v2</p>")

    def test_missing_file_builds_directly(self):
        missing = Path(self.tmp.name) / "nope.md"
        out = render_cache.render("BookA", missing, lambda: "<p>x</p>")
        self.assertEqual(out, "<p>x</p>")

    def test_lru_entry_limit(self):
        render_cache._MAX_ENTRIES = 3
        calls = {}

        def mk(i):
            fp = self._mkfile("f%d.md" % i, "x" * (i + 1))
            calls[i] = 0

            def build(i=i):
                calls[i] += 1
                return "<p>%d</p>" % i

            return fp, build

        files = [mk(i) for i in range(4)]
        for fp, build in files:
            render_cache.render("B", fp, build)
        self.assertEqual(render_cache.stats()["entries"], 3, "超过条数上限应淘汰到 3")
        # 第一个（最旧）应已被淘汰 → 再访问重新 build
        render_cache.render("B", files[0][0], files[0][1])
        self.assertEqual(calls[0], 2, "最旧条目应被 LRU 淘汰后重建")

    def test_byte_budget_eviction(self):
        render_cache._MAX_BYTES = 1000

        def mk(i):
            fp = self._mkfile("big%d.md" % i, "x" * 10)

            def build(i=i):
                return "A" * 600

            return fp, build

        for i in range(3):
            fp, build = mk(i)
            render_cache.render("B", fp, build)
        self.assertLessEqual(render_cache.stats()["bytes"], 1000, "字节预算内保留条目")


if __name__ == "__main__":
    unittest.main(verbosity=2)
