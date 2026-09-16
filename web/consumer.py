#!/usr/bin/env python3
"""P3b cron 消费者辅助脚本：把队列扫描/批次计划/记录/进度全部脚本化，
cron agent 只负责 delegate_task 翻译（LLM 部分）。

用法:
  consumer.py pick
      找最老 ready 任务，输出本批要译的 chunks（含术语表存在性/邻居上下文提示）
  consumer.py finish <job_id> <chunk_id...>
      本批收尾：record + merge_meta + 进度 + 完成判断
  consumer.py request-merge <job_id> <title>
      全部译完后写 merge_request.json + 状态 merging（Web watcher 线程执行 merge）
  consumer.py status <job_id>
      单任务快照（job + progress + plan 摘要）

输出均为 stdout JSON。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ---- 路径常量（不依赖 cwd，cron agent 可在任何目录跑）----
WORK_ROOT = Path("/root/translate/work")
JOBS_ROOT = WORK_ROOT / "jobs"
SCRIPT_DIR = Path("/root/translate/book/scripts")
PYTHON = "/root/translate/book/.venv/bin/python"

BATCH_SIZE = 3  # 每批 chunk 数（delegate_task 并发上限）
MIN_AVAILABLE_MB = 1024  # 可用内存下限（< 1GB 不取任务）


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, *args], capture_output=True, text=True, timeout=120)


def _load_json(fp: Path, default=None):
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return default


def _job_fp(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "job.json"


def _progress_fp(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "progress.json"


def get_job(job_id: str) -> dict | None:
    return _load_json(_job_fp(job_id))


def write_progress(job_id: str, stage: str, message: str, done: int = 0, total=None):
    d = {"stage": stage, "message": message, "chunks_done": done,
         "chunks_total": total, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _progress_fp(job_id).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def update_job(job_id: str, **fields):
    job = get_job(job_id)
    if job is None:
        return None
    job.update(fields)
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _job_fp(job_id).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def run_state_plan(temp_dir: str) -> dict:
    r = _run(str(SCRIPT_DIR / "run_state.py"), "plan", temp_dir)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "")[-500:]}
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"error": f"plan 输出非 JSON: {r.stdout[-500:]}"}


def pick() -> dict:
    if mem_available_mb() < MIN_AVAILABLE_MB:
        return {"task": None, "reason": "low_memory",
                "available_mb": mem_available_mb()}
    # 找最老的非终态任务：优先 ready；translating（中断残留）也续
    candidates = []
    for d in sorted(JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime):
        if not d.is_dir():
            continue
        job = _load_json(d / "job.json")
        if job is None:
            continue
        if job.get("status") in ("ready", "translating"):
            candidates.append(job)
    if not candidates:
        return {"task": None, "reason": "no_ready_task"}
    job = candidates[0]  # 最老
    job_id = job["id"]
    temp_dir = str(WORK_ROOT / job["temp_dir"]) if job.get("temp_dir") else None
    if not temp_dir or not Path(temp_dir).is_dir():
        update_job(job_id, status="failed", error="temp_dir 不存在")
        return {"task": None, "reason": "temp_dir_missing", "job_id": job_id}

    plan = run_state_plan(temp_dir)
    if "error" in plan:
        update_job(job_id, status="failed", error=plan["error"])
        return {"task": None, "reason": "plan_error", "job_id": job_id, "error": plan["error"]}

    to_translate = plan["translation_chunk_ids"]
    record_only = plan["record_only_chunk_ids"]
    if record_only:
        # output 已存在但未记录（如 mock/中断残留）→ 先 record，再重新 plan
        _run(str(SCRIPT_DIR / "run_state.py"), "record", temp_dir, *record_only)
        plan = run_state_plan(temp_dir)
        if "error" in plan:
            update_job(job_id, status="failed", error=plan["error"])
            return {"task": None, "reason": "plan_error", "job_id": job_id, "error": plan["error"]}
        to_translate = plan["translation_chunk_ids"]
        record_only = plan["record_only_chunk_ids"]
    if not to_translate and not record_only:
        # 已全部译完（无待译、无待记录）——触发 merge 流程
        return {"task": {"job_id": job_id, "status": job["status"], "temp_dir": temp_dir,
                         "all_done": True, "original_title": _read_original_title(temp_dir),
                         "chunk_count": _chunk_count(temp_dir)}}
    batch = to_translate[:BATCH_SIZE]
    # 记录 chunk 总数（manifest），回写 job.json
    total = _chunk_count(temp_dir)
    if total:
        update_job(job_id, chunk_count=total)
    # translating 状态由 cron agent 置（或这里直接置）
    update_job(job_id, status="translating")
    # chunks_done = plan 里 unchanged 数（已稳定的块）
    done = len(plan["unchanged_chunk_ids"])
    write_progress(job_id, "translating", f"正在翻译本批 {len(batch)} 块...", done=done, total=total)
    return {"task": {"job_id": job_id, "status": "translating", "temp_dir": temp_dir,
                     "batch_chunk_ids": batch, "record_only_chunk_ids": record_only,
                     "remaining": len(to_translate), "chunk_count": total,
                     "all_done": False}}


def _chunk_count(temp_dir: str) -> int | None:
    m = _load_json(Path(temp_dir) / "manifest.json")
    if m:
        return m.get("chunk_count")
    return None


def _read_original_title(temp_dir: str) -> str:
    try:
        for line in (Path(temp_dir) / "config.txt").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("original_title"):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def finish(job_id: str, chunk_ids: list[str]) -> dict:
    job = get_job(job_id)
    if job is None:
        return {"error": "任务不存在"}
    if job.get("status") == "cancelled":
        return {"job_id": job_id, "status": "cancelled", "message": "已取消，跳过收尾"}
    temp_dir = str(WORK_ROOT / job["temp_dir"])
    if not Path(temp_dir).is_dir():
        return {"error": f"temp_dir 不存在: {temp_dir}"}

    # 1. record 本批
    r = _run(str(SCRIPT_DIR / "run_state.py"), "record", temp_dir, *chunk_ids)
    record_err = "" if r.returncode == 0 else (r.stderr or r.stdout or "")[-300:]

    # 2. merge_meta（无人值守：不采纳术语决策，只记录 consumed）
    prep = _run(str(SCRIPT_DIR / "merge_meta.py"), "prepare-merge", temp_dir)
    consumed = []
    if prep.returncode == 0:
        try:
            p = json.loads(prep.stdout)
            consumed = p.get("consumed_chunk_ids", [])
        except Exception:
            consumed = []
    if consumed:
        doc = json.dumps({"auto_apply": [], "decisions": [], "consumed_chunk_ids": consumed})
        _run(str(SCRIPT_DIR / "merge_meta.py"), "apply-merge", temp_dir, input=doc)

    # 3. 进度 + 完成判断
    plan = run_state_plan(temp_dir)
    if "error" in plan:
        return {"error": plan["error"]}
    total = _chunk_count(temp_dir)
    done = len(plan["unchanged_chunk_ids"])
    remaining = len(plan["translation_chunk_ids"])
    all_done = remaining == 0 and not plan["record_only_chunk_ids"]

    if all_done:
        update_job(job_id, status="merging")
        write_progress(job_id, "merging", "全部块已译完，准备合并构建...", done=total, total=total)
        return {"job_id": job_id, "status": "merging", "all_done": True,
                "chunks_done": total, "chunks_total": total,
                "original_title": _read_original_title(temp_dir),
                "record_error": record_err or None}
    # 还有剩余 → 保持 ready 下轮续
    update_job(job_id, status="ready")
    write_progress(job_id, "ready", f"本批完成，剩余 {remaining} 块待译", done=done, total=total)
    return {"job_id": job_id, "status": "ready", "all_done": False,
            "chunks_done": done, "chunks_total": total, "remaining": remaining,
            "next_batch": plan["translation_chunk_ids"][:BATCH_SIZE],
            "record_error": record_err or None}


def request_merge(job_id: str, title: str) -> dict:
    job = get_job(job_id)
    if job is None:
        return {"error": "任务不存在"}
    temp_dir = str(WORK_ROOT / job["temp_dir"])
    req = {"title": title, "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (JOBS_ROOT / job_id / "merge_request.json").write_text(
        json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    update_job(job_id, status="merging")
    write_progress(job_id, "merging", "合并请求已提交，等待构建...",
                   done=job.get("chunk_count") or 0, total=job.get("chunk_count") or 0)
    return {"job_id": job_id, "status": "merging", "temp_dir": temp_dir, "title": title}


def status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        return {"error": "任务不存在"}
    out = {"job": job, "progress": _load_json(_progress_fp(job_id))}
    if job.get("temp_dir"):
        plan = run_state_plan(str(WORK_ROOT / job["temp_dir"]))
        if "error" not in plan:
            out["plan"] = {"to_translate": len(plan["translation_chunk_ids"]),
                           "record_only": len(plan["record_only_chunk_ids"]),
                           "unchanged": len(plan["unchanged_chunk_ids"])}
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pick")
    p_fin = sub.add_parser("finish")
    p_fin.add_argument("job_id")
    p_fin.add_argument("chunk_ids", nargs="+")
    p_merge = sub.add_parser("request-merge")
    p_merge.add_argument("job_id")
    p_merge.add_argument("title")
    p_st = sub.add_parser("status")
    p_st.add_argument("job_id")
    args = ap.parse_args()

    if args.cmd == "pick":
        print(json.dumps(pick(), ensure_ascii=False))
    elif args.cmd == "finish":
        print(json.dumps(finish(args.job_id, args.chunk_ids), ensure_ascii=False))
    elif args.cmd == "request-merge":
        print(json.dumps(request_merge(args.job_id, args.title), ensure_ascii=False))
    elif args.cmd == "status":
        print(json.dumps(status(args.job_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
