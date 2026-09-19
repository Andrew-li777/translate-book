#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览分发（preview.py）与设置项（settings）单元测试 —— P2-2 双源协议。

覆盖：开关关/未配置不重写、书未同步不重写、已同步+开关开才重写、
设置项字段类型收敛（bool/str、去尾斜杠）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview  # noqa: E402
import settings  # noqa: E402


class TestPreviewRewrite(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self._saved = preview.STATE_FP
        preview.STATE_FP = Path(self.td.name) / "state.json"
        preview.invalidate()
        self.addCleanup(self._restore)

    def _restore(self):
        preview.STATE_FP = self._saved
        preview.invalidate()

    def _write_state(self, ready):
        rec = {"complete": True} if ready else {"complete": False, "pending": 3}
        # sig 用真实签名（tmp 下无书 → 0.0 也为「一致」；ready 需 complete+sig 相等）
        preview.STATE_FP.write_text(
            json.dumps({"books": {"BookA": dict(rec, sig=preview._book_sig("BookA"))}}),
            encoding="utf-8",
        )
        preview.invalidate()

    HTML = '<img loading="lazy" src="/api/books/BookA/file/images/000001.png">'

    def _load(self, flag=True, base="https://preview.example/"):
        return mock.patch.object(settings, "load", return_value={"preview_r2": flag, "preview_base": base})

    def test_noop_when_flag_off(self):
        self._write_state(True)
        with self._load(flag=False):
            out = preview.rewrite(self.HTML, "BookA")
        self.assertIn("/api/books/", out)

    def test_noop_when_base_empty(self):
        self._write_state(True)
        with self._load(base=""):
            out = preview.rewrite(self.HTML, "BookA")
        self.assertIn("/api/books/", out)

    def test_noop_when_not_ready(self):
        self._write_state(False)
        with self._load():
            out = preview.rewrite(self.HTML, "BookA")
        self.assertIn("/api/books/", out)

    def test_rewrite_when_ready(self):
        self._write_state(True)
        with self._load():
            out = preview.rewrite(self.HTML, "BookA")
        self.assertIn("https://preview.example/books/BookA/images/000001.png", out)
        self.assertNotIn("/api/books/BookA/file/", out)

    def test_only_images_path_rewritten(self):
        self._write_state(True)
        html = ('<img src="/api/books/BookA/file/images/a.png">'
                '<a href="/api/books/BookA/file/output.md">md</a>')
        with self._load():
            out = preview.rewrite(html, "BookA")
        self.assertIn("https://preview.example/books/BookA/images/a.png", out)
        self.assertIn("/api/books/BookA/file/output.md", out, "非图片路径不动")


class TestSettingsCoerce(unittest.TestCase):
    def test_preview_fields(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "s.json"
            with mock.patch.object(settings, "_FP", fp):
                d = settings.save({"preview_r2": 1, "preview_base": "https://p.example/"})
                self.assertIs(d["preview_r2"], True)
                self.assertEqual(d["preview_base"], "https://p.example", "应去掉尾斜杠")
                d2 = settings.save({"preview_r2": ""})
                self.assertIs(d2["preview_r2"], False)
                self.assertEqual(d2["preview_base"], "https://p.example", "未提及的键保留")


if __name__ == "__main__":
    unittest.main(verbosity=2)
