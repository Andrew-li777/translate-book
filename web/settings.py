#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translate-web 运行期设置（文件持久化，改完立即生效、不需重启）

当前项：
  force_local_download  强制下载走本地直连（跳过 R2 预签名）
                        —— 排障/验证用：怀疑 R2 链路有问题时打开，确认下载不受影响

存储于 /root/translate/work/.web_settings.json（工作区内，随工作区一起迁移；
不进仓库，避免把服务器状态提交到公开仓库）。
"""
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("settings")

_FP = Path("/root/translate/work/.web_settings.json")
_DEFAULTS = {"force_local_download": False}


def load() -> dict:
    d = dict(_DEFAULTS)
    try:
        raw = json.loads(_FP.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d.update({k: raw[k] for k in _DEFAULTS if k in raw})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("读取设置失败（用默认值）: %s", e)
    return d


def save(patch: dict) -> dict:
    d = load()
    d.update({k: v for k, v in (patch or {}).items() if k in _DEFAULTS})
    d["force_local_download"] = bool(d["force_local_download"])
    try:
        _FP.parent.mkdir(parents=True, exist_ok=True)
        _FP.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("写入设置失败: %s", e)
    return d


def force_local() -> bool:
    return bool(load().get("force_local_download"))


# ===================== 下载计数（S6-B：服务端自记，近似下载量）=====================
# 说明：预签名 URL 是**本地计算**，不产生 R2 API 调用，因此真实用量只能接
# CF GraphQL Analytics（需 API Token）。这里自记「签发次数」作为近似指标。
_STATS_FP = Path("/root/translate/work/.web_stats.json")


def _stats_load() -> dict:
    try:
        d = json.loads(_STATS_FP.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def bump_presign() -> None:
    """签发一次 R2 预签名 URL 计数 +1（永不抛异常，绝不影响下载）"""
    try:
        d = _stats_load()
        d["presign_count"] = int(d.get("presign_count", 0)) + 1
        d["presign_last"] = time.time()
        _STATS_FP.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FP.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("下载计数写入失败: %s", e)


def presign_stats() -> dict:
    d = _stats_load()
    return {"count": int(d.get("presign_count", 0)),
            "last": d.get("presign_last") or 0}
