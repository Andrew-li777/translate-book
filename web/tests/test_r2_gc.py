#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""孤儿清理（books.storage_gc）三守卫单元测试 —— 对应审计「GC 拒删」验收面。

隔离方式：books.WORK_ROOT 指向临时目录、r2.objects_all / r2.delete_objects 全 mock，
不触碰生产工作区与 R2。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import books  # noqa: E402
import r2  # noqa: E402


class TestStorageGcGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        book_dir = root / "GoodBook_temp"
        book_dir.mkdir()
        (book_dir / "book.pdf").write_bytes(b"x" * 100)     # 本地成品存在
        self._saved_root = books.WORK_ROOT
        books.WORK_ROOT = root
        self.addCleanup(self._restore)

        self.remote = {"GoodBook": {"book.pdf": 100}, "Ghost": {"orphan.pdf": 5}}

    def _restore(self):
        books.WORK_ROOT = self._saved_root

    def test_invalid_path_refused(self):
        with mock.patch.object(r2, "objects_all", return_value=self.remote), \
             mock.patch.object(r2, "delete_objects", return_value=0) as dele:
            res = books.storage_gc(["/etc/passwd", "books"])
        self.assertEqual(res["deleted"], 0)
        self.assertTrue(all("路径不合法" in x["why"] for x in res["refused"]))
        dele.assert_not_called()

    def test_local_exists_refused(self):
        """守卫 1：本地存在同名成品 → 一律拒绝（GC 期间的补传安全）。"""
        with mock.patch.object(r2, "objects_all", return_value=self.remote), \
             mock.patch.object(r2, "delete_objects", return_value=0) as dele:
            res = books.storage_gc(["books/GoodBook/book.pdf"])
        self.assertEqual(res["deleted"], 0)
        self.assertIn("本地存在同名成品", res["refused"][0]["why"])
        dele.assert_not_called()

    def test_remote_gone_refused(self):
        """守卫 2：远端复核发现对象已不在 → 拒绝。"""
        with mock.patch.object(r2, "objects_all", return_value=self.remote), \
             mock.patch.object(r2, "delete_objects", return_value=0) as dele:
            res = books.storage_gc(["books/Ghost/already-gone.pdf"])
        self.assertEqual(res["deleted"], 0)
        self.assertIn("远端已无此对象", res["refused"][0]["why"])
        dele.assert_not_called()

    def test_valid_orphan_deleted(self):
        with mock.patch.object(r2, "objects_all", return_value=self.remote) as objs, \
             mock.patch.object(r2, "delete_objects", return_value=1) as dele:
            res = books.storage_gc(["books/Ghost/orphan.pdf"])
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["files"], ["books/Ghost/orphan.pdf"])
        dele.assert_called_once_with(["books/Ghost/orphan.pdf"])
        # 守卫 3：删前必须强制刷新远端复核（force=True 不可省）
        objs.assert_called_once()
        self.assertEqual(objs.call_args.kwargs.get("force"), True)

    def test_unreachable_aborts(self):
        """R2 读不到 → 整体放弃，不冒删错风险。"""
        with mock.patch.object(r2, "objects_all", return_value=None), \
             mock.patch.object(r2, "delete_objects", return_value=0) as dele:
            res = books.storage_gc(["books/Ghost/orphan.pdf"])
        self.assertIn("R2 读不到", res["error"])
        dele.assert_not_called()

    def test_input_limits(self):
        with mock.patch.object(r2, "objects_all", return_value=self.remote), \
             mock.patch.object(r2, "delete_objects", return_value=0):
            res_empty = books.storage_gc([])
            self.assertIn("未指定对象", res_empty["error"])
            res_big = books.storage_gc(["books/X/f%d" % i for i in range(501)])
            self.assertIn("500", res_big["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
