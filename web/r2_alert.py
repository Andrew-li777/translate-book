#!/usr/bin/env python3
"""R2 同步异常守护：差异持续存在时告警，正常时**完全静默**。

设计（配合 Hermes no_agent cron）：
  - 正常（差异 0）→ stdout 为空 → cron 不投递任何消息（用户零打扰）
  - 异常持续 ≥ 30 分钟 → stdout 输出告警文本 → 原样投递到 QQ
  - 恢复 → 输出一条「已恢复」后清空状态
  - 条件签名变化 或 距上次告警 ≥ 6h → 才重复告警（防刷屏）
  - 未配置 R2 凭证 → 静默退出（功能未启用，不是故障）
  - 任何未预期异常 → 输出「守护脚本自身异常」并同样去重（宁可报错也不要静默失效）

状态文件：/root/translate/work/.r2_alert_state.json
用法：
  r2_alert.py                # 正常巡检（cron 用）
  r2_alert.py --selftest diff|unreachable|clean --age 3600   # 自检（注入假条件）
  r2_alert.py --json         # 输出结构化快照（排障用，不写状态）
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

STATE_FP = Path("/root/translate/work/.r2_alert_state.json")
SUSTAIN_SEC = 30 * 60      # 需持续 ≥30 分钟才告警
REPEAT_SEC = 6 * 3600      # 同一问题最多每 6 小时提醒一次
SYNC_HINT = "r2_sync.py（或等 15 分钟一次的 translate-r2sync 定时任务）"


# ----------------------------- 工具 -----------------------------
def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0
    return "%dB" % n


def _load_state() -> dict:
    try:
        return json.loads(STATE_FP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FP.parent.mkdir(parents=True, exist_ok=True)
        STATE_FP.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("⚠️ translate-web 存储守护无法写入状态文件：%s" % e, file=sys.stderr)


def _sig(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ----------------------------- 巡检 -----------------------------
def collect() -> dict:
    """返回 r2.snapshot 视图（强制绕过 30s 缓存，保证是本次实时结果）"""
    import books
    import r2

    bs = books.scan_books()
    for b in bs:
        t = b.get("dir") or (b["name"] + "_temp")
        b["_temp_dir"] = t
    local_map = {b["name"]: books._final_files(b.get("files") or {}) for b in bs}
    local_map = {k: v for k, v in local_map.items() if v}
    r2._snap.update({"ts": 0.0, "key": None, "data": None})   # 强制刷新
    return r2.snapshot(local_map)


def judge(view: dict) -> tuple:
    """把 snapshot 判定成一个条件 → (kind, sig, text)

    kind: None(正常) / "diff" / "unreachable" / "noconfig"
    """
    if not view.get("configured"):
        return None, "noconfig", ""
    if not view.get("available"):
        return "unreachable", "unreachable", (
            "⚠️ translate-web 存储守护：R2 元数据读取失败\n\n"
            "本地成品 %d 个 / %s 正常，但列举 R2 对象失败（凭证错误、网络中断或 bucket 不可达）。\n\n"
            "影响：网站下载会自动回退到本地直连（不影响使用），但成品归档已停。\n"
            "排查：确认 /root/.translate-r2-cred 三行凭证有效、R2 控制台服务正常。"
            % (view["totals"]["local_files"], _fmt_bytes(view["totals"]["local_bytes"]))
        )

    t = view["totals"]
    if not t["diff_items"]:
        return None, "clean", ""

    lines = []
    for name, d in sorted(view["books"].items()):
        if not (d["missing"] or d["mismatch"] or d["extra"]):
            continue
        parts = []
        if d["missing"]:
            parts.append("缺 %d 项（%s）" % (len(d["missing"]), "、".join(d["missing"][:3])))
        if d["mismatch"]:
            parts.append("尺寸不符 %d 项（%s）" % (len(d["mismatch"]), "、".join(d["mismatch"][:3])))
        if d["extra"]:
            parts.append("多余 %d 项（%s）" % (len(d["extra"]), "、".join(d["extra"][:3])))
        lines.append("· 《%s》 %s" % (name, "，".join(parts)))

    text = (
        "⚠️ translate-web 成品归档不一致（已持续 >30 分钟）\n\n"
        "本地成品 %d 个 / %s ｜ R2 %d 个对象 / %s ｜ 差异 %d 项\n\n%s\n\n"
        "影响：缺失/不符的成品下载时会自动回退到本地直连，**用户不受影响**；"
        "但 R2 上的副本不是最新。\n"
        "处理：跑一次 %s 即可增量补齐（幂等，已同步的会跳过）。"
        % (t["local_files"], _fmt_bytes(t["local_bytes"]),
           t["r2_objects"], _fmt_bytes(t["r2_bytes"]), t["diff_items"],
           "\n".join(lines[:8]), SYNC_HINT)
    )
    return "diff", _sig(json.dumps(
        {k: [v["missing"], v["mismatch"], v["extra"]] for k, v in view["books"].items()},
        sort_keys=True)), text


# ----------------------------- 决策 -----------------------------
def decide(state: dict, kind, sig, text, now: float, sustain: int):
    """返回 (输出文本 or None, 新状态)"""
    st = dict(state)
    pend = st.get("pending") or {}
    alerted = st.get("alerted") or {}

    # ---- 正常：若此前告过警，发一条恢复通知 ----
    if kind is None:
        if alerted:
            st.pop("alerted", None)
            st.pop("pending", None)
            if sig == "noconfig":
                return None, st          # 凭证被移除 → 静默，不用「已恢复」打扰
            return "✅ translate-web 成品归档已恢复正常，R2 与本地一致（差异 0 项）。", st
        st.pop("pending", None)
        return None, st

    # ---- 异常：先记 pending，持续够久才告警 ----
    if pend.get("kind") != kind or pend.get("sig") != sig:
        pend = {"kind": kind, "sig": sig, "since": now}
    st["pending"] = pend

    if now - pend["since"] < sustain:
        return None, st                  # 首次发现：静默观察，等下轮确认

    if alerted.get("sig") == sig and now - alerted.get("ts", 0) < REPEAT_SEC:
        return None, st                  # 已告过且未到重提间隔

    st["alerted"] = {"kind": kind, "sig": sig, "ts": now}
    return text, st


# ----------------------------- 主流程 -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出快照 JSON（只读，不写状态）")
    ap.add_argument("--selftest", choices=["diff", "unreachable", "clean"],
                    help="注入假条件自检")
    ap.add_argument("--age", type=int, default=0, help="假装异常已持续 N 秒")
    ap.add_argument("--no-state", action="store_true", help="不读写状态文件")
    a = ap.parse_args()

    now = time.time()
    try:
        view = collect()
        kind, sig, text = judge(view)
    except Exception as e:
        view, kind = {}, "crash"
        sig = _sig("crash:%s:%s" % (type(e).__name__, e))
        text = ("⚠️ translate-web 存储守护脚本自身异常（归档状态未知）：\n%s: %s\n\n"
                "守护脚本没生效期间，同步异常不会被提醒。请检查 /root/translate/web/r2_alert.py。"
                % (type(e).__name__, e))

    if a.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0

    if a.selftest:
        if a.selftest == "clean":
            kind, sig, text = None, "clean", ""
        elif a.selftest == "diff":
            kind = "diff"
            sig = _sig("selftest-diff")
            text = ("⚠️ translate-web 成品归档不一致（已持续 >30 分钟）\n\n"
                    "[自检注入] 本地 12 个 / 207.4MB ｜ R2 11 个 / 164.9MB ｜ 差异 1 项\n\n"
                    "· 《Adventure-of-Sherlock-Holmes》 缺 1 项（book.docx）")
        else:
            kind, sig, text = "unreachable", "unreachable", "[自检注入] R2 元数据读取失败"

    state = {} if a.no_state else _load_state()
    if a.age:
        state["pending"] = {"kind": kind, "sig": sig, "since": now - a.age}
    out, new_state = decide(state, kind, sig, text, now, SUSTAIN_SEC)
    if not a.no_state:
        new_state["last_run"] = now
        _save_state(new_state)

    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
