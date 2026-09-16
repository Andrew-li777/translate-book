#!/usr/bin/env python3
"""translate-web 配置"""
import os
from pathlib import Path

# 工作区：存放所有 <书名>_temp/ 目录
WORK_ROOT = Path(os.environ.get("TRANSLATE_WORK", "/root/translate/work"))
# translate-book 脚本目录
SCRIPT_DIR = Path("/root/translate/book/scripts")
# venv python
PYTHON = "/root/translate/book/.venv/bin/python"

SERVICE_NAME = "translate-web"
VERSION = "0.1.0"