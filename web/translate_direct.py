#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D 方案：脚本直连 amd API 的翻译消费者。

在 cron+delegate_task（A 方案）基础上提速：省去子代理冷启动整个 agent 会话的
开销，直接用 HTTP 调用 amd 通道的 chat/completions，ThreadPool 并发 3。

复用 consumer.py 的 pick/finish/request-merge/队列/进度逻辑，不改 Web 侧。

架构:
  主循环（sleep POLL_SEC 轮询）:
    ① consumer.pick() 找最老 ready/translating 任务，取本批 batch_chunk_ids
    ② ThreadPool(N) 并发调 amd API 翻译 batch 内每个 chunk:
       · prompt = 固定翻译 system/user 模板 + 术语表 + 邻居上下文
       · 超时 TIMEOUT / 失败重试 RETRIES 次 / 指数退避
       · 写 <temp>/output_chunk{i}.md
    ③ consumer.finish(job_id, chunk_ids) 收尾: record + merge_meta + 进度
    ④ 若 all_done -> consumer.request_merge(job_id, 译名) 交由 Web watcher merge
  循环直到队列无 ready 任务后 sleep，等新任务。

日志: /root/translate/work/direct.log（追加，含每块耗时/ token 数）

用法:
  python3 translate_direct.py            # 后台长跑守护
  python3 translate_direct.py --once     # 只跑一轮（测一块）
  python3 translate_direct.py --once --chunks chunk0001   # 指定块（A/B 测试）
"""
import argparse
import json
import os
import random
import sys
import time
import traceback
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---- 路径常量 ----
WEB_DIR = Path("/root/translate/web")
SCRIPT_DIR = Path("/root/translate/book/scripts")
WORK_ROOT = Path("/root/translate/work")
PYTHON = "/root/translate/book/.venv/bin/python"
LOG_FP = WORK_ROOT / "direct.log"

# ---- amd API 配置（从 .env 读 key，不硬编码/不显示）----
API_URL = "https://developer.amd.com.cn/radeon/api/v1/chat/completions"
MODEL = "DeepSeek-V4-Flash"
KEY_NAME = "AMD_RADEON_API_KEY"

# ---- 并发/重试 ----
BATCH = 3            # 并行 API 调用数（与 consumer.BATCH_SIZE 一致，保守）
TIMEOUT = 180        # 单请求超时秒
RETRIES = 3          # 失败重试次数
BACKOFF_BASE = 15    # 指数退避基数秒
POLL_SEC = 10        # 队列轮询间隔
TARGET_LANG = "zh"   # 目标语言（简体中文）

# 翻译用 none 思考：A/B 实测 reasoning_effort=medium 会让小 chunk 花 141-144s（1536 reasoning tokens），
# minimal 直接 180s 超时；而 none 只要 54-68s 且质量等同（甚至章节标题翻得更准）。翻译是直译任务，
# 不需要推理。可用环境变量 DIRECT_REASONING 覆盖。
REASONING_EFFORT = os.environ.get("DIRECT_REASONING", "none")

# 导入 consumer 复用其队列/记录/进度逻辑
sys.path.insert(0, str(WEB_DIR))
import consumer  # noqa: E402
import fix_images  # noqa: E402  (图片引用后处理，merge 前补回缺失图片)

# ---- 日志 ----
def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_FP, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def get_amd_key() -> str:
    for line in open("/root/.hermes/.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith(KEY_NAME + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"未找到 {KEY_NAME}")

KEY = get_amd_key()


# ---- 翻译 prompt 模板（逐字复制 skill 子代理 context 模板）----
SYSTEM_PROMPT = (
    "你是书籍翻译流水线的翻译引擎，只负责把一个 chunk 的 Markdown 从源语言翻译成简体中文。"
    "你直接输出译文正文，不输出任何说明、注释、对话或 Markdown 代码围栏。"
    "严格遵循用户消息中的硬性规则。"
)

def build_user_prompt(chunk_md: str, chunk_id: str, terms: str, neighbor_ctx: str) -> str:
    lines = []
    lines.append("硬性规则：")
    lines.append("1. 严格保持 Markdown 格式不变（标题/链接/图片引用）")
    lines.append("2. 仅翻译文字，保留所有 Markdown 语法和文件名")
    lines.append("3. 不要删除独立数字行（年份/章节编号/引用编号可能是正文）")
    lines.append("4. 只输出译文正文，无任何说明/注释/对话")
    lines.append("5. 保留所有 ![alt](path) 图片引用：路径不修改，alt 可译")
    lines.append("6. 原始 HTML 标签属性（alt/title）内文本中的 \"→“”、'→‘’，避免撑断属性；src/href 结构属性值不改")
    lines.append("7. 智能识别多级标题：书名/章节 #，大节 ##，小节 ### 依次降级；不盲目加标题标记")
    lines.append("8. 引用块（>）/诗歌/行末 \\ 保留格式只译内容")
    if terms.strip():
        lines.append(f"9. 术语表：以下术语必须用指定译法：\n{terms.strip()}")
    if neighbor_ctx.strip():
        lines.append(f"10. 邻居上下文（只读，勿翻译勿写入输出）：\n{neighbor_ctx.strip()}")
    lines.append(f"目标语言：简体中文（{TARGET_LANG}）")
    lines.append("")
    lines.append("待翻译源 chunk（保持 Markdown 原样）：")
    lines.append("---")
    lines.append(chunk_md)
    return "\n".join(lines)


def run_script(*args, input_text=None, timeout=120):
    """调用 translate-book 脚本，返回 CompletedProcess 或 None（失败）"""
    import subprocess
    try:
        return subprocess.run([str(PYTHON), *args], capture_output=True, text=True,
                              input=input_text, timeout=timeout)
    except Exception:
        return None


def glossary_terms(temp_dir: str, chunk_md: str) -> str:
    r = run_script(str(SCRIPT_DIR / "glossary.py"), "print-terms-for-chunk", temp_dir, chunk_md)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return ""


def neighbor_context(temp_dir: str, chunk_md: str) -> str:
    r = run_script(str(SCRIPT_DIR / "chunk_context.py"), temp_dir, chunk_md)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return ""


def api_translate(chunk_id: str, temp_dir: str, chunk_md: str) -> tuple[str, str, dict]:
    """调用 amd API 翻译一个 chunk。返回 (output_text, chunk_id, stats)。"""
    terms = glossary_terms(temp_dir, chunk_id + ".md")
    ctx = neighbor_context(temp_dir, chunk_id + ".md")
    user_prompt = build_user_prompt(chunk_md, chunk_id, terms, ctx)

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4096,
        "reasoning_effort": REASONING_EFFORT,
    }
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + KEY}

    last_err = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            t0 = time.time()
            r = urllib.request.urlopen(req, timeout=TIMEOUT)
            d = json.loads(r.read())
            msg = ((d.get("choices") or [{}])[0].get("message")) or {}
            text = (msg.get("content") or "").strip()
            u = d.get("usage") or {}
            stats = {
                "chunk": chunk_id, "latency_s": round(time.time() - t0, 1),
                "completion_tokens": u.get("completion_tokens", "?"),
                "reasoning_tokens": u.get("reasoning_tokens", "?"),
                "total_tokens": u.get("total_tokens", "?"),
                "attempts": attempt + 1,
            }
            if not text:
                raise RuntimeError(f"空响应内容: {str(d)[:300]}")
            return text, chunk_id, stats
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code == 429 and attempt < RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 5))
                continue
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 5))
                continue
            break
    raise RuntimeError(f"翻译失败 {chunk_id}: {last_err}")


def translate_one(chunk_id: str, temp_dir: str) -> dict:
    """翻译单个 chunk 并写 output_chunk{i}.md，返回 stats。"""
    src = Path(temp_dir) / f"{chunk_id}.md"
    out = Path(temp_dir) / f"output_{chunk_id}.md"
    chunk_md = src.read_text(encoding="utf-8")
    text, _, stats = api_translate(chunk_id, temp_dir, chunk_md)
    out.write_text(text + "\n", encoding="utf-8")
    # 图片引用后处理：模型翻译长/插图密集块时可能丢图片引用，merge 会因 image validation
    # 失败而卡住。用确定性脚本把源 chunk 缺失的图片引用补回，避免 merge 失败。
    try:
        n = fix_images.fix(temp_dir, chunk_id)
        if n and n > 0:
            log(f"    🔧 {chunk_id} 补回 {n} 个缺失图片引用")
    except Exception as e:
        log(f"    ⚠ 图片修复异常（忽略）: {e}")
    stats["out_chars"] = len(out.read_text(encoding="utf-8").strip())
    log(f"  ✓ {chunk_id} 译完 ({stats['out_chars']} 字, {stats['latency_s']}s, {stats['completion_tokens']} tok)")
    return stats


def process_task(task: dict) -> bool:
    """处理一个 pick 任务。返回是否已处理过（否则队列空）。"""
    job_id = task["job_id"]
    temp_dir = task["temp_dir"]

    # all_done -> request merge
    if task.get("all_done"):
        title = task.get("original_title") or "译书"
        consumer.request_merge(job_id, title)
        log(f"  ✅ {job_id} 全部译完，已提交合并（标题：{title}）")
        return True

    batch = task.get("batch_chunk_ids") or []
    if not batch:
        return True

    log(f"▶ 任务 {job_id}：本批翻译 {len(batch)} 块（剩余 {task.get('remaining')}）")
    results = {}
    with ThreadPoolExecutor(max_workers=BATCH) as ex:
        futs = {ex.submit(translate_one, cid, temp_dir): cid for cid in batch}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                stats = fut.result()
                results[cid] = stats
            except Exception as e:
                log(f"  ✗ {cid} 翻译失败: {e}")
                # 失败块跳过，交给下一轮重试（不写 output，plan 会继续选它）

    # 只有成功写出 output 的块才 finish
    done_ids = list(results.keys())
    if done_ids:
        fin = consumer.finish(job_id, done_ids)
        if fin.get("error"):
            log(f"  ⚠ finish 报错: {fin['error']}")
        else:
            if fin.get("all_done"):
                # finish 只置 status=merging，但不写 merge_request.json —— watcher 不合并。
                # 需显式调 request_merge 写 merge_request.json + 置 merging，才能触发 Web watcher 构建。
                title = (task.get("original_title") or "")
                # original_title 是英文原文；这里不翻书名，直接用原文 + 标记，避免再花一次 LLM。
                # （可选：翻书名需要一次额外 API 调用，此处为最小改动先用原文。）
                title = title or "译书"
                consumer.request_merge(job_id, title)
                log(f"  ✅ {job_id} 全部译完，已写 merge_request 触发合并（title={title}）")
            else:
                log(f"  → {job_id} 进度 {fin.get('chunks_done')}/{fin.get('chunks_total')}，剩余 {fin.get('remaining')} 块")
    return True


def run_once(specific_chunk: str | None = None):
    """跑一轮。specific_chunk 用于 A/B 测试指定块。"""
    if specific_chunk:
        # 手动模式：找一个含该 chunk 的任务
        for d in sorted(consumer.JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime):
            job = consumer.get_job(d.name)
            if not job or job.get("status") not in ("ready", "translating"):
                continue
            temp_dir = str(WORK_ROOT / job["temp_dir"]) if job.get("temp_dir") else None
            if temp_dir and Path(temp_dir).is_dir() and Path(temp_dir, f"{specific_chunk}.md").exists():
                stats = translate_one(specific_chunk, temp_dir)
                consumer.finish(job["id"], [specific_chunk])
                log(f"手动单块完成: {json.dumps(stats, ensure_ascii=False)}")
                return True
        log(f"未找到含 {specific_chunk} 的 ready 任务")
        return False

    p = consumer.pick()
    if p.get("task") is None:
        log(f"轮询：{p.get('reason')}，{POLL_SEC}s 后重试")
        return False
    return process_task(p["task"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--chunks", help="指定 chunk（配合 --once 做 A/B 测试）")
    args = ap.parse_args()

    log(f"══ translate_direct 启动 (model={MODEL}, reasoning={REASONING_EFFORT}, BATCH={BATCH}) ══")
    if args.chunks:
        run_once(args.chunks)
        return
    if args.once:
        run_once()
        return
    # 长跑守护
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log("收到中断，退出")
            break
        except Exception:
            log("主循环异常:\n" + traceback.format_exc())
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()