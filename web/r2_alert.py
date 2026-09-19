#!/usr/bin/env python3
"""存储守护（S3 + S6-C/B）：异常持续告警 + 每日容量采样，正常时**完全静默**。

设计（配合 Hermes no_agent cron，每 30 分钟）：
  - 正常 → stdout 为空 → cron 不投递任何消息（用户零打扰）
  - 任一异常持续 ≥ 30 分钟 → 输出告警文本 → 原样投递到 QQ
  - 恢复 → 输出一条「已恢复」后清空状态
  - 条件签名变化 或 距上次告警 ≥ 6h → 才重复告警（防刷屏）
  - 未配置 R2 凭证 → 静默退出（功能未启用，不是故障）
  - 脚本自身崩溃 → 告警（不能静默失效），同样去重

监护条件（可同时命中，合并成一条告警）：
  1. diff         本地成品 vs R2 对象不一致（缺 / 尺寸不符 / 多余）
  2. unreachable  R2 元数据读不到（凭证/网络/bucket 故障）
  3. disk         服务器磁盘使用率 > 85%（机器级：盘满会连带 trs/scan 全挂）
  4. quota        R2 用量 > 免费额度的 80%
  5. hist         容量采样断更 > 36h（采样链自身失效的兜底）

每日采样（S6-B，与守护同进程 → 零新增 cron/脚本）：
  每个自然日首次运行时写一行到 work/.storage_history.jsonl（**只统计 trs 自己**）：
     {date, trs_bytes, local_files, local_bytes, r2_objects, r2_bytes, presign}
  R2 读不到时 r2_* 写 None（**绝不写 0**，否则趋势图出现假跌）。

用法：
  r2_alert.py                     # 正常巡检（cron 用；顺带当日采样）
  r2_alert.py --json              # 输出实时快照（只读，不写状态/不采样）
  r2_alert.py --selftest diff,disk --age 3600   # 自检注入（可多条件，逗号分隔）
  r2_alert.py --history PATH --state PATH       # 测试用隔离文件
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_WORK = Path("/root/translate/work")
STATE_FP = _WORK / ".r2_alert_state.json"
HIST_FP = _WORK / ".storage_history.jsonl"

SUSTAIN_SEC = 30 * 60        # 需持续 ≥30 分钟才告警
REPEAT_SEC = 6 * 3600        # 同一问题最多每 6 小时提醒一次
DISK_WARN_PCT = 85.0         # 机器级磁盘告警阈值
R2_QUOTA_WARN_PCT = 80.0     # R2 免费额度告警阈值
HIST_STALE_SEC = 36 * 3600   # 采样断更判定
HIST_KEEP_DAYS = 180         # 历史保留条数
SYNC_HINT = "r2_sync.py（或等 15 分钟一次的 translate-r2sync 定时任务）"


# ----------------------------- 通用工具 -----------------------------
def _fmt_bytes(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f%s" % (n, unit)) if unit != "B" else ("%dB" % int(n))
        n /= 1024.0
    return "%dB" % int(n)


def _load_json(fp: Path) -> dict:
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_json(fp: Path, d: dict) -> None:
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("⚠️ 存储守护无法写入 %s：%s" % (fp, e), file=sys.stderr)


def _sig(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ----------------------------- 采集 -----------------------------
def collect() -> dict:
    """返回 r2.snapshot 视图（强制绕过 30s 缓存，保证是本次实时结果）"""
    import books
    import r2

    bs = books.scan_books()
    local_map = {b["name"]: books._final_files(b.get("files") or {}) for b in bs}
    local_map = {k: v for k, v in local_map.items() if v}
    r2._snap.update({"ts": 0.0, "key": None, "data": None})   # 强制刷新
    return r2.snapshot(local_map)


def _dir_bytes() -> int:
    """trs 自己的工作区占用（**只统计 trs，不含 scan**）"""
    total = 0
    try:
        import books
        total = books._dir_bytes(books.WORK_ROOT)
    except Exception:
        pass
    return total


def _extra(work_bytes: int, hist_fp: Path) -> dict:
    """告警判定需要的补充指标"""
    import books
    du = books._disk_info()
    hist = _read_history(hist_fp)
    last_ts = 0.0
    if hist and hist[-1].get("date"):
        try:
            last_ts = time.mktime(time.strptime(hist[-1]["date"] + " 00:00:00", "%Y-%m-%d %H:%M:%S")) + 86400
        except Exception:
            last_ts = 0.0
    return {"work_bytes": work_bytes, "disk": du, "hist": hist, "hist_last_ts": last_ts}


# ----------------------------- 采样历史 -----------------------------
def _read_history(fp: Path) -> list:
    rows = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception as e:
        print("⚠️ 读取容量历史失败：%s" % e, file=sys.stderr)
    return rows


def _write_history(fp: Path, rows: list) -> None:
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(fp)


def sample(view: dict, extra: dict, now: float, fp: Path = HIST_FP, force: bool = False):
    """当日首次运行写一行采样。返回 (是否写入, rows)。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    rows = _read_history(fp)
    if rows and rows[-1].get("date") == today and not force:
        return False, rows
    try:
        import settings
        presign = settings.presign_stats().get("count", 0)
    except Exception:
        presign = None
    available = bool(view.get("available"))
    t = view.get("totals", {})
    row = {
        "date": today,
        "trs_bytes": extra.get("work_bytes", 0),                       # 只统计 trs 自己
        "local_files": t.get("local_files", 0),
        "local_bytes": t.get("local_bytes", 0),
        # R2 读不到 → None（不能写 0，否则趋势图假跌）
        "r2_objects": t.get("r2_objects", 0) if available else None,
        "r2_bytes": t.get("r2_bytes", 0) if available else None,
        "presign": presign,
    }
    if rows and rows[-1].get("date") == today:
        rows[-1] = row            # force 覆盖当天
    else:
        rows.append(row)
    rows = rows[-HIST_KEEP_DAYS:]
    try:
        _write_history(fp, rows)
    except Exception as e:
        print("⚠️ 写入容量历史失败（%s）：%s" % (fp, e), file=sys.stderr)
        return False, rows
    return True, rows


# ----------------------------- 判定 -----------------------------
def _c_diff(view) -> tuple:
    t = view["totals"]
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
    text = ("【成品归档不一致】\n"
            "本地成品 %d 个 / %s ｜ R2 %d 个对象 / %s ｜ 差异 %d 项\n\n%s\n\n"
            "影响：缺失/不符的成品下载时会自动回退本地直连，**用户不受影响**；但 R2 副本不是最新。\n"
            "处理：跑一次 %s 即可增量补齐（幂等，已同步的会跳过）。"
            % (t["local_files"], _fmt_bytes(t["local_bytes"]), t["r2_objects"],
               _fmt_bytes(t["r2_bytes"]), t["diff_items"], "\n".join(lines[:8]), SYNC_HINT))
    sub = _sig(json.dumps({k: [v["missing"], v["mismatch"], v["extra"]]
                           for k, v in view["books"].items()}, sort_keys=True))
    return "diff", sub, text


def _c_unreachable(view) -> tuple:
    t = view["totals"]
    return "unreachable", "unreachable", (
        "【R2 元数据读取失败】\n"
        "本地成品 %d 个 / %s 正常，但列举 R2 对象失败（凭证错误、网络中断或 bucket 不可达）。\n\n"
        "影响：网站下载会自动回退本地直连（不影响使用），但成品归档已停、当日容量采样写空值。\n"
        "排查：确认 /root/.translate-r2-cred 三行凭证有效、R2 控制台服务正常。"
        % (t["local_files"], _fmt_bytes(t["local_bytes"])))


def _c_disk(extra) -> tuple:
    du = extra["disk"]
    pct = du.get("pct", 0)
    return "disk", _sig("disk:%d" % int(pct)), (
        "【服务器磁盘接近满】已用 %.1f%%（阈值 %.0f%%）\n"
        "磁盘 %s / %s（剩 %s）。\n\n"
        "影响：**盘满会同时打断 translate-web、D 引擎、scan 解析与 R2 归档**。\n"
        "处理：清 scan 视频临时目录 / /root/translate/work/.trash / 各 *_temp 的中间产物。"
        % (pct, DISK_WARN_PCT, _fmt_bytes(du.get("used")), _fmt_bytes(du.get("total")),
           _fmt_bytes(du.get("free"))))


def _c_quota(view) -> tuple:
    try:
        import books
        quota = books.R2_FREE_QUOTA
    except Exception:
        quota = 10 * 1024 ** 3
    used = view["totals"]["r2_bytes"]
    pct = used * 100.0 / quota if quota else 0
    return "quota", _sig("quota:%d" % int(pct)), (
        "【R2 免费额度接近上限】已用 %.1f%%（阈值 %.0f%%）\n"
        "R2 %s / %s（%d 个对象）。\n\n"
        "影响：超出免费额度将开始计费（存储 $0.015/GB·月，egress 仍免费）。\n"
        "处理：在「存储」页清理孤儿对象，或把老书成品改为只留本地。"
        % (pct, R2_QUOTA_WARN_PCT, _fmt_bytes(used), _fmt_bytes(quota),
           view["totals"]["r2_objects"]))


def _c_hist(extra, now: float) -> tuple:
    rows = extra["hist"]
    last = rows[-1] if rows else {}
    age_h = (now - extra["hist_last_ts"]) / 3600.0 if extra.get("hist_last_ts") else 0
    return "hist", _sig("hist:%d" % int(age_h)), (
        "【容量采样已断更 %.0f 小时】\n最后一条采样：%s\n\n"
        "影响：存储页趋势图与「容量变化」判断会失真。\n"
        "排查：守护 cron 是否仍在跑、`/root/translate/work/.storage_history.jsonl` 是否可写。"
        % (age_h, last.get("date", "(无)")))


def judge(view: dict, extra: dict, now: float) -> tuple:
    """把所有命中条件合并成一个 (kind, sig, text)。

    kind: None(正常) / "noconfig"(静默) / "diff" / "unreachable" / "disk" / "quota" / "hist"
          / 多条件时用 "+" 连接（如 "diff+disk"）
    """
    if not view.get("configured"):
        return None, "noconfig", ""
    if not view.get("available"):
        k, s, t = _c_unreachable(view)
        return k, s, t

    conds = []
    if view["totals"]["diff_items"]:
        conds.append(_c_diff(view))
    if extra["disk"].get("pct", 0) > DISK_WARN_PCT:
        conds.append(_c_disk(extra))
    try:
        import books
        if view["totals"]["r2_bytes"] * 100.0 / books.R2_FREE_QUOTA > R2_QUOTA_WARN_PCT:
            conds.append(_c_quota(view))
    except Exception:
        pass
    if extra.get("hist_last_ts") and (now - extra["hist_last_ts"]) > HIST_STALE_SEC:
        conds.append(_c_hist(extra, now))

    if not conds:
        return None, "clean", ""

    kind = "+".join(sorted(c[0] for c in conds))
    sig = _sig("|".join("%s:%s" % (c[0], c[1]) for c in sorted(conds)))
    if len(conds) == 1:
        text = conds[0][2]
    else:
        text = ("⚠️ translate-web 存储守护：%d 项异常（已持续 >30 分钟）\n\n" % len(conds)) \
               + "\n\n————\n\n".join(c[2] for c in conds)
    return kind, sig, text


# ----------------------------- 决策（状态机）-----------------------------
def decide(state: dict, kind, sig, text, now: float, sustain: int):
    """返回 (输出文本 or None, 新状态)"""
    st = dict(state)
    pend = st.get("pending") or {}
    alerted = st.get("alerted") or {}

    if kind is None:                        # ---- 正常 ----
        if alerted:
            st.pop("alerted", None)
            st.pop("pending", None)
            if sig == "noconfig":
                return None, st              # 凭证被移除 → 静默，不用「已恢复」打扰
            return "✅ translate-web 存储守护：全部恢复正常（成品归档一致、磁盘与 R2 额度正常）。", st
        st.pop("pending", None)
        return None, st

    if pend.get("kind") != kind or pend.get("sig") != sig:   # 首次发现/条件变化 → 重新观察
        pend = {"kind": kind, "sig": sig, "since": now}
    st["pending"] = pend
    if now - pend["since"] < sustain:
        return None, st
    if alerted.get("sig") == sig and now - alerted.get("ts", 0) < REPEAT_SEC:
        return None, st
    st["alerted"] = {"kind": kind, "sig": sig, "ts": now}
    return text, st


# ----------------------------- 自检注入 -----------------------------
def _inject(names, view, extra, now):
    conds = []
    for n in names:
        if n == "diff":
            conds.append(("diff", _sig("selftest-diff"),
                          "【成品归档不一致】[自检注入]\n本地 12 个 / 207.4MB ｜ R2 11 个 / 164.9MB ｜ 差异 1 项\n\n"
                          "· 《Adventure-of-Sherlock-Holmes》 缺 1 项（book.docx）"))
        elif n == "unreachable":
            conds.append(("unreachable", "unreachable", "【R2 元数据读取失败】[自检注入]"))
        elif n == "disk":
            conds.append(("disk", _sig("selftest-disk"),
                          "【服务器磁盘接近满】[自检注入]已用 91.5%%（阈值 %.0f%%）" % DISK_WARN_PCT))
        elif n == "quota":
            conds.append(("quota", _sig("selftest-quota"),
                          "【R2 免费额度接近上限】[自检注入]已用 88.0%%（阈值 %.0f%%）" % R2_QUOTA_WARN_PCT))
        elif n == "hist":
            conds.append(("hist", _sig("selftest-hist"), "【容量采样已断更 48 小时】[自检注入]"))
        elif n == "clean":
            return None, "clean", ""
    if not conds:
        return None, "clean", ""
    kind = "+".join(sorted(c[0] for c in conds))
    sig = _sig("|".join("%s:%s" % (c[0], c[1]) for c in sorted(conds)))
    text = conds[0][2] if len(conds) == 1 else (
        ("⚠️ translate-web 存储守护：%d 项异常（已持续 >30 分钟）\n\n" % len(conds))
        + "\n\n————\n\n".join(c[2] for c in conds))
    return kind, sig, text


# ----------------------------- 主流程 -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出快照 JSON（只读：不写状态、不采样）")
    ap.add_argument("--selftest", help="注入假条件自检：diff/unreachable/disk/quota/hist/clean，逗号分隔")
    ap.add_argument("--age", type=int, default=0, help="假装异常已持续 N 秒")
    ap.add_argument("--no-state", action="store_true", help="不读写状态文件（也不采样）")
    ap.add_argument("--no-sample", action="store_true", help="跳过当日采样")
    ap.add_argument("--force-sample", action="store_true", help="强制重写当日采样行")
    ap.add_argument("--state", default=str(STATE_FP), help="状态文件路径（测试用）")
    ap.add_argument("--history", default=str(HIST_FP), help="采样历史路径（测试用）")
    a = ap.parse_args()

    state_fp, hist_fp = Path(a.state), Path(a.history)
    now = time.time()
    work_bytes = _dir_bytes()

    # ---- 采集 + 判定 ----
    view, extra = {}, {}
    try:
        view = collect()
        extra = _extra(work_bytes, hist_fp)
        if a.selftest:
            kind, sig, text = _inject([s.strip() for s in a.selftest.split(",") if s.strip()],
                                      view, extra, now)
        else:
            kind, sig, text = judge(view, extra, now)
    except Exception as e:
        view, kind = {}, "crash"
        sig = _sig("crash:%s:%s" % (type(e).__name__, e))
        text = ("⚠️ translate-web 存储守护脚本自身异常（归档状态未知）：\n%s: %s\n\n"
                "守护脚本没生效期间，同步异常不会被提醒。请检查 /root/translate/web/r2_alert.py。"
                % (type(e).__name__, e))

    if a.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0

    # ---- 每日采样（真实巡检模式才写；与守护同进程 → 零新增 cron）----
    if not (a.no_state or a.no_sample or a.selftest):
        wrote, rows = sample(view, extra, now, hist_fp, force=a.force_sample)
        if wrote:
            print("↻ 已记录当日容量采样：%s（%d 天历史）"
                  % (rows[-1].get("date"), len(rows)), file=sys.stderr)

    # ---- 状态机 ----
    state = {} if a.no_state else _load_json(state_fp)
    if a.age:
        state["pending"] = {"kind": kind, "sig": sig, "since": now - a.age}
    out, new_state = decide(state, kind, sig, text, now, SUSTAIN_SEC)
    if not a.no_state:
        new_state["last_run"] = now
        _save_json(state_fp, new_state)

    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
