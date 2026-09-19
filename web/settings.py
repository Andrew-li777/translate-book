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
