#!/usr/bin/env bash
# translate-web 回归套件一键运行（P1-1）
cd "$(dirname "$0")/.." || exit 1
exec /root/translate/book/.venv/bin/python -m unittest discover -s tests -v "$@"
