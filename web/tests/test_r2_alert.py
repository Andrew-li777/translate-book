#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储守护（r2_alert）状态机测试 —— 对应审计「守护状态机 5 条件」验收面。

两层：
  1. 进程内单元：judge 判定边界（磁盘 84.9/85.1、额度 79/81、断更 35h/37h、
     多条件合并）+ decide 状态机（观察期 / 去抖 / 恢复 / noconfig 静默）。
  2. 子进程集成：--selftest 真跑一遍 CLI（状态/历史全部隔离到 tmp，
     对 R2 仅做只读列举，不写任何生产文件）。
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import books  # noqa: E402
import r2_alert  # noqa: E402

WEB = Path(__file__).resolve().parents[1]
PY = "/root/translate/book/.venv/bin/python"
NOW = time.time()


def _view(diff=0, r2_bytes=0, configured=True, available=True):
    books_map = {}
    if diff:
        books_map = {"B": {"missing": ["book.docx"], "mismatch": [], "extra": []}}
    return {
        "configured": configured, "available": available,
        "totals": {"local_files": 1, "local_bytes": 10, "r2_objects": 1,
                   "r2_bytes": r2_bytes, "diff_items": diff},
        "books": books_map,
    }


def _extra(disk_pct=0.0, hist_last_ts=0):
    hist = [{"date": "2026-09-10"}] if hist_last_ts else []
    return {"work_bytes": 0, "disk": {"pct": disk_pct}, "hist": hist,
            "hist_last_ts": hist_last_ts}


class TestJudge(unittest.TestCase):
    def test_clean(self):
        kind, sig, text = r2_alert.judge(_view(), _extra(), NOW)
        self.assertIsNone(kind)
        self.assertEqual(sig, "clean")

    def test_disk_boundary(self):
        self.assertIsNone(r2_alert.judge(_view(), _extra(disk_pct=84.9), NOW)[0])
        kind = r2_alert.judge(_view(), _extra(disk_pct=85.1), NOW)[0]
        self.assertEqual(kind, "disk")

    def test_quota_boundary(self):
        quota = books.R2_FREE_QUOTA
        self.assertIsNone(r2_alert.judge(_view(r2_bytes=int(quota * 0.79)), _extra(), NOW)[0])
        kind = r2_alert.judge(_view(r2_bytes=int(quota * 0.81)), _extra(), NOW)[0]
        self.assertEqual(kind, "quota")

    def test_hist_boundary(self):
        self.assertIsNone(r2_alert.judge(_view(), _extra(hist_last_ts=NOW - 35 * 3600), NOW)[0])
        kind = r2_alert.judge(_view(), _extra(hist_last_ts=NOW - 37 * 3600), NOW)[0]
        self.assertEqual(kind, "hist")

    def test_diff_and_merge(self):
        kind, sig, text = r2_alert.judge(_view(diff=1), _extra(), NOW)
        self.assertEqual(kind, "diff")
        self.assertIn("成品归档不一致", text)
        # 多条件合并：表头「N 项异常」
        kind2, _, text2 = r2_alert.judge(_view(diff=1), _extra(disk_pct=90), NOW)
        self.assertEqual(kind2, "diff+disk")
        self.assertIn("2 项异常", text2)

    def test_unreachable(self):
        kind, _, text = r2_alert.judge(_view(available=False), _extra(), NOW)
        self.assertEqual(kind, "unreachable")
        self.assertIn("R2", text)


class TestDecide(unittest.TestCase):
    def test_observation_window(self):
        out, st = r2_alert.decide({}, "disk", "s1", "T", NOW, 1800)
        self.assertIsNone(out, "首次发现应静默观察")
        self.assertEqual(st["pending"]["kind"], "disk")

    def test_alert_after_sustain(self):
        state = {"pending": {"kind": "disk", "sig": "s1", "since": NOW - 3600}}
        out, st = r2_alert.decide(state, "disk", "s1", "T", NOW, 1800)
        self.assertEqual(out, "T")
        self.assertEqual(st["alerted"]["sig"], "s1")

    def test_dedup_within_6h(self):
        state = {"pending": {"kind": "disk", "sig": "s1", "since": NOW - 3600},
                 "alerted": {"kind": "disk", "sig": "s1", "ts": NOW - 100}}
        out, _ = r2_alert.decide(state, "disk", "s1", "T", NOW, 1800)
        self.assertIsNone(out, "同签名 6h 内应去抖静默")

    def test_repeat_after_6h(self):
        state = {"pending": {"kind": "disk", "sig": "s1", "since": NOW - 3600},
                 "alerted": {"kind": "disk", "sig": "s1", "ts": NOW - 7 * 3600}}
        out, _ = r2_alert.decide(state, "disk", "s1", "T", NOW, 1800)
        self.assertEqual(out, "T", "超过 6h 应再次提醒")

    def test_condition_change_reopens(self):
        """条件签名变化 → 重新进入观察期（防两问题互相掩盖）。"""
        state = {"pending": {"kind": "disk", "sig": "s1", "since": NOW - 3600},
                 "alerted": {"kind": "disk", "sig": "s1", "ts": NOW - 100}}
        out, st = r2_alert.decide(state, "diff+disk", "s2", "T", NOW, 1800)
        self.assertIsNone(out)
        self.assertEqual(st["pending"]["since"], NOW)

    def test_recovery(self):
        state = {"pending": {"kind": "disk", "sig": "s1", "since": NOW - 3600},
                 "alerted": {"kind": "disk", "sig": "s1", "ts": NOW - 3600}}
        out, st = r2_alert.decide(state, None, "clean", "", NOW, 1800)
        self.assertIn("恢复正常", out)
        self.assertNotIn("alerted", st)


class TestSelftestInject(unittest.TestCase):
    def test_inject_merged(self):
        kind, _, text = r2_alert._inject(["diff", "disk"], {}, {}, NOW)
        self.assertEqual(kind, "diff+disk")
        self.assertIn("2 项异常", text)
        self.assertIn("[自检注入]", text)

    def test_inject_clean(self):
        kind, sig, text = r2_alert._inject(["clean"], {}, {}, NOW)
        self.assertIsNone(kind)
        self.assertEqual(text, "")


class TestCliIntegration(unittest.TestCase):
    """子进程真跑 CLI（隔离状态/历史；只读生产数据）。"""

    def _run(self, args, state_dir):
        cmd = [PY, str(WEB / "r2_alert.py")] + args + [
            "--state", str(state_dir / "state.json"),
            "--history", str(state_dir / "hist.jsonl"),
        ]
        return subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True, timeout=180)

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 1) 首次发现 + 假装持续 1h → 告警
            r1 = self._run(["--selftest", "diff", "--age", "3600"], d)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("成品归档不一致", r1.stdout)
            self.assertIn("[自检注入]", r1.stdout)
            # 2) 同签名 6h 内 → 静默（去抖）
            r2 = self._run(["--selftest", "diff", "--age", "3600"], d)
            self.assertEqual(r2.stdout.strip(), "", "同签名应去抖静默")
            # 3) 恢复 → 一条「已恢复」
            r3 = self._run(["--selftest", "clean", "--age", "0"], d)
            self.assertIn("恢复正常", r3.stdout)
            # 4) 新状态、未满观察期 → 静默
            with tempfile.TemporaryDirectory() as td2:
                r4 = self._run(["--selftest", "diff", "--age", "60"], Path(td2))
                self.assertEqual(r4.stdout.strip(), "", "未满 30 分钟应静默")
            # 5) 多条件合并
            with tempfile.TemporaryDirectory() as td3:
                r5 = self._run(["--selftest", "diff,disk", "--age", "3600"], Path(td3))
                self.assertIn("2 项异常", r5.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
