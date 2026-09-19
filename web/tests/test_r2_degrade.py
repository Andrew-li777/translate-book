#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R2 降级静默单元测试 —— 对应审计「R2 降级」验收面。

把 r2._CRED_FILE 指向不存在路径 → 全链路应「静默降级」（configured=False），
绝不抛异常、绝不阻断主流程。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import books  # noqa: E402
import r2  # noqa: E402
import r2_alert  # noqa: E402

MISSING_CRED = Path(tempfile.gettempdir()) / "no-such-r2-cred-for-tests"


class TestR2Degrade(unittest.TestCase):
    def _no_cred(self):
        return mock.patch.object(r2, "_CRED_FILE", MISSING_CRED)

    def test_unconfigured_silent_fallbacks(self):
        with self._no_cred():
            self.assertFalse(r2.enabled())
            self.assertIsNone(r2.objects_all())
            self.assertFalse(r2.upload("X", "book.pdf", Path("/etc/hostname")))
            self.assertIsNone(r2.presign("X", "book.pdf", "book.pdf"))
            self.assertEqual(r2.delete_book("X"), 0)
            self.assertIsNone(r2.book_objects("X"))

    def test_snapshot_and_storage_view_degrade(self):
        with self._no_cred():
            view = r2.snapshot({"X": {"book.pdf": 123}})
            self.assertFalse(view["configured"])
            self.assertFalse(view["available"])
            self.assertEqual(view["books"], {})
            # storage_view 不抛异常，返回空视图
            v2 = books.storage_view([{"name": "X", "files": {"book.pdf": 123}}])
            self.assertFalse(v2["configured"])
            self.assertFalse(v2["available"])

    def test_alert_noconfig_is_silent(self):
        """守护：凭证未配置 → judge 返回 noconfig；带旧 alerted 状态恢复也静默。"""
        view = {"configured": False, "available": False,
                "totals": {"local_files": 0, "local_bytes": 0, "r2_objects": 0,
                           "r2_bytes": 0, "diff_items": 0}, "books": {}}
        extra = {"work_bytes": 0, "disk": {"pct": 0}, "hist": [], "hist_last_ts": 0}
        kind, sig, text = r2_alert.judge(view, extra, time.time())
        self.assertIsNone(kind)
        self.assertEqual(sig, "noconfig")
        self.assertEqual(text, "")
        # 之前处于告警态 → 凭证被移除时静默清状态（不用「已恢复」打扰）
        out, st = r2_alert.decide({"alerted": {"kind": "diff", "sig": "x", "ts": 0}},
                                  None, "noconfig", "", time.time(), 1800)
        self.assertIsNone(out)
        self.assertNotIn("alerted", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
