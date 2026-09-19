#!/usr/bin/env python3
"""translate-web 任务队列管理：job.json + progress.json，文件系统状态机。

状态机（P3）：
  pending → converting → ready → translating → merging → done
             └─ failed ─────────────────────────┘
  ready/translating → cancelled
"""
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from config import WORK_ROOT

JOBS_ROOT = WORK_ROOT / "jobs"

# 允许上传的类型
ALLOWED_EXTS = {".pdf", ".docx", ".epub"}
MAX_UPLOAD_BYTES = 95 * 1024 * 1024  # 95MB —— P1-2：CF 免费版请求体上限 100MB，留 multipart 开销余量

# 状态机合法迁移（宽松：允许任何更新，校验放业务层）
STATUS_ALL = {"pending", "converting", "ready", "translating", "merging",
              "done", "failed", "cancelled"}
TERMINAL = {"done", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def ensure_root() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+", job_id):
        raise ValueError(f"非法 job_id: {job_id}")
    return JOBS_ROOT / job_id


def _job_fp(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _progress_fp(job_id: str) -> Path:
    return _job_dir(job_id) / "progress.json"


def new_job_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)


def sanitize_title(title: str) -> str:
    """书名安全化：去路径危险字符，用于 input 文件名和 temp 目录名"""
    t = re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("_.")
    return t[:60] or "book"


def create_job(title: str, ext: str, target_lang: str = "zh") -> dict:
    """创建任务，返回 job dict"""
    ensure_root()
    job_id = new_job_id()
    safe = sanitize_title(title)
    filename = f"{safe}{ext}"
    job = {
        "id": job_id,
        "title": title.strip(),
        "filename": filename,
        "target_lang": target_lang,
        "status": "pending",
        "temp_dir": None,
        "chunk_count": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    _write_json(_job_fp(job_id), job)
    _write_json(_progress_fp(job_id), {
        "stage": "pending", "message": "任务已创建，等待转换",
        "chunks_done": 0, "chunks_total": None, "updated_at": _now(),
    })
    return job


def _write_json(fp: Path, obj: dict) -> None:
    fp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def save_input(job_id: str, data: bytes) -> Path:
    """保存上传文件到 job 目录，返回路径（旧接口，保留；web 上传已改为流式落盘）"""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"任务不存在: {job_id}")
    fp = _job_dir(job_id) / job["filename"]
    fp.write_bytes(data)
    return fp


def input_target(job_id: str) -> Path:
    """上传落盘目标路径（<job_dir>/<filename>），供流式写入（P2-1）"""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"任务不存在: {job_id}")
    return _job_dir(job_id) / job["filename"]


def discard(job_id: str) -> None:
    """上传失败清理：移除整个任务目录（best-effort，绝不抛出）"""
    try:
        d = _job_dir(job_id)
    except ValueError:
        return
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def get_job(job_id: str) -> dict | None:
    fp = _job_fp(job_id)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_job(job_id: str, **fields) -> dict | None:
    job = get_job(job_id)
    if job is None:
        return None
    for k, v in fields.items():
        job[k] = v
    job["updated_at"] = _now()
    _write_json(_job_fp(job_id), job)
    return job


def write_progress(job_id: str, stage: str, message: str,
                   chunks_done: int = 0, chunks_total: int | None = None) -> None:
    d = {
        "stage": stage, "message": message,
        "chunks_done": chunks_done, "chunks_total": chunks_total,
        "updated_at": _now(),
    }
    _write_json(_progress_fp(job_id), d)


def read_progress(job_id: str) -> dict:
    fp = _progress_fp(job_id)
    if not fp.exists():
        return {"stage": "pending", "message": "", "chunks_done": 0,
                "chunks_total": None, "updated_at": ""}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {"stage": "pending", "message": "进度读取失败", "chunks_done": 0,
                "chunks_total": None, "updated_at": ""}


def list_jobs(limit: int = 50) -> list[dict]:
    """任务列表（新→旧），附带进度"""
    ensure_root()
    jobs = []
    for d in sorted(JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir() or not (d / "job.json").exists():
            continue
        job = get_job(d.name)
        if job is None:
            continue
        job["progress"] = read_progress(d.name)
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def input_path(job_id: str) -> Path | None:
    job = get_job(job_id)
    if job is None:
        return None
    fp = _job_dir(job_id) / job["filename"]
    return fp if fp.is_file() else None


def cancel(job_id: str) -> dict | None:
    """取消：仅对非终态任务生效（打 cancel 标记，cron 批次间检查）"""
    job = get_job(job_id)
    if job is None:
        return None
    if job["status"] in TERMINAL:
        return job
    job = update_job(job_id, status="cancelled")
    write_progress(job_id, "cancelled", "任务已取消")
    return job
