#!/usr/bin/env python3
"""translate-web FastAPI 应用"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import books
import config
import jobs

app = FastAPI(title="Translate Book Web", version="0.1.0")
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# 前端可安全读取/下载的文本扩展
_TEXT_MIME = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
}
_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
             ".bmp": "image/bmp"}
_DOWNLOAD_MIME = {
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip", ".zip": "application/zip", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".mp4": "video/mp4",
}


@app.get("/", response_class=HTMLResponse)
def index():
    return (_STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/books")
def api_books():
    books.ensure_work_root()
    return {"books": books.scan_books()}


@app.get("/api/books/{name}")
def api_book(name: str):
    b = books.get_book(name)
    if b is None:
        raise HTTPException(404, "书不存在")
    return b


@app.get("/api/books/{name}/chunk/{cid}")
def api_chunk(name: str, cid: str):
    """返回单个 chunk 的原文 + 译文 + 邻居上下文标题"""
    b = books.get_book(name)
    if b is None:
        raise HTTPException(404, "书不存在")
    # 找到该 chunk 的索引，用于邻居
    idx = next((i for i, c in enumerate(b["chunks"]) if c["id"] == cid), None)
    if idx is None:
        raise HTTPException(404, "chunk 不存在")
    d = Path("/root/translate/work") / b["dir"]
    src_fp = d / f"{cid}.md"
    out_fp = d / f"output_{cid}.md"
    meta_fp = d / f"output_{cid}.meta.json"
    prev_id = b["chunks"][idx - 1]["id"] if idx > 0 else None
    next_id = b["chunks"][idx + 1]["id"] if idx < len(b["chunks"]) - 1 else None
    return {
        "id": cid,
        "index": idx + 1,
        "total": len(b["chunks"]),
        "prev": prev_id,
        "next": next_id,
        "src": src_fp.read_text(encoding="utf-8", errors="replace") if src_fp.exists() else "",
        "trans": out_fp.read_text(encoding="utf-8", errors="replace") if out_fp.exists() else None,
        "meta": json.loads(meta_fp.read_text(encoding="utf-8")) if meta_fp.exists() else None,
    }


@app.get("/api/books/{name}/file/{path:path}")
def api_file(name: str, path: str, raw: bool = Query(False)):
    """读取文件：文本返回内容(json)，图片/二进制返回文件流"""
    fp = books.resolve_file(name, path)
    if fp is None:
        raise HTTPException(404, "文件不存在")
    mime = _TEXT_MIME.get(fp.suffix.lower())
    if mime and not raw:
        return {"name": fp.name, "path": path, "mime": mime,
                "content": fp.read_text(encoding="utf-8", errors="replace")}
    # 内嵌预览：html 以 text/html 返回（无 attachment），供 iframe 直接渲染；
    # 图片相对路径重写为绝对路径（经 file 端点），修复 iframe 内裂图
    if fp.suffix.lower() == ".html":
        raw_html = fp.read_bytes().decode("utf-8", errors="replace")
        return Response(_absolutize_img_src(raw_html, name).encode("utf-8"),
                        media_type="text/html; charset=utf-8")
    if fp.suffix.lower() in _IMG_MIME:
        return FileResponse(fp, media_type=_IMG_MIME[fp.suffix.lower()])
    return Response(fp.read_bytes(), media_type=_DOWNLOAD_MIME.get(fp.suffix.lower(), "application/octet-stream"),
                    headers={"Content-Disposition": f'attachment; filename="{fp.name}"'})


@app.get("/api/books/{name}/render/{path:path}")
def api_render(name: str, path: str):
    """用 Python markdown 渲染 md 文件为 HTML（前端直接内嵌）"""
    import markdown as md
    fp = books.resolve_file(name, path)
    if fp is None or fp.suffix.lower() != ".md":
        raise HTTPException(404, "文件不存在")
    text = fp.read_text(encoding="utf-8", errors="replace")
    html = md.markdown(_strip_pandoc_attrs(text), extensions=["extra", "tables", "fenced_code", "sane_lists"])
    # 图片相对路径重写为绝对路径（经 file 端点），修复页面内裂图
    html = _absolutize_img_src(html, name)
    return {"html": html, "name": fp.name, "path": path}


_PANDOC_HEADER = re.compile(r"(?m)^(#{1,6}.*?)\s*\{[^\n}]*\}\s*$")
_PANDOC_IMG = re.compile(r"(!\[[^\]]*\]\([^)]*\))\s*\{[^\n}]*\}")
# [text]{.i} -> *text*（剥内部星号防嵌套斜体破损）
_PANDOC_EM = re.compile(r"\[([^\]]+)\]\{\.(?:i|em|italic)\}")
# "text"{.i} -> *text*（无方括号的裸属性）
_PANDOC_EM2 = re.compile(r'"([^"]*)"\{\.(?:i|em|italic)\}')
_PANDOC_INLINE = re.compile(r"\[([^\]]+)\]\{[^}]*\}")
# 未闭合方括号: blockquote 行首 [ 且行内无 ] -> 剥掉 [ 保留 >
_UNCLOSED = re.compile(r"(?m)^(\s*)>(?:\s*)?\[(?=[^\[\]\n]*$)")
# 孤立方括号 [text]（后面不跟 (url)）— Calibre 诗行分组语法，剥掉外层括号保留文本
_BARE_BRACKET = re.compile(r"\[([^\]]+)\](?!\()")
# 行尾反斜杠（Calibre 硬换行标记）-> 两个空格（markdown 硬换行，渲染 <br>）
_TRAIL_BS = re.compile(r"(?m)\\+\s*$")
# 图片相对路径 -> 绝对路径：src="images/xxx.png" -> src="/api/books/{name}/file/images/xxx.png"
_IMG_SRC_RE = re.compile(r'(<img[^>]*?\ssrc=["\'])(?!https?:|/|data:|blob:|#)([^"\']+)(["\'])', re.I)


def _absolutize_img_src(html: str, name: str) -> str:
    """把 HTML 里相对路径图片重写为绝对路径（经 file 端点），修复 iframe/页面内裂图。"""
    def _repl(m):
        src = m.group(2)
        clean = src[2:] if src.startswith("./") else src
        return f'{m.group(1)}/api/books/{name}/file/{clean}{m.group(3)}'
    return _IMG_SRC_RE.sub(_repl, html)


def _em_repl(m):
    """[text]{.i} / "text"{.i} -> *text*，剥掉内部星号防嵌套破损"""
    return "*" + m.group(1).replace("*", "") + "*"


def _strip_pandoc_attrs(text):
    """去掉 Calibre/EPUB 转换产物中的 Pandoc 属性语法残留与诗行格式标记：
    ## 标题 {#id .class k="v"} → ## 标题
    ![alt](url){.class} → ![alt](url)
    [text]{.i} / "text"{.i} → *text*（保留斜体语义）；[text]{.attr} → text
    [诗行] → 诗行（剥孤立方括号，不破坏 [链接](url)）
    行尾反斜杠 \ → 硬换行（<br>），诗行保持独立行
    """
    text = _PANDOC_HEADER.sub(r"\1", text)
    text = _PANDOC_IMG.sub(r"\1", text)
    text = _PANDOC_EM.sub(_em_repl, text)
    text = _PANDOC_EM2.sub(_em_repl, text)
    text = _PANDOC_INLINE.sub(r"\1", text)
    text = _UNCLOSED.sub(r"\1> ", text)
    text = _BARE_BRACKET.sub(r"\1", text)
    text = _TRAIL_BS.sub("  ", text)
    return text


@app.get("/api/books/{name}/download/{path:path}")
def api_download(name: str, path: str):
    """下载文件（所有类型）"""
    fp = books.resolve_file(name, path)
    if fp is None:
        raise HTTPException(404, "文件不存在")
    mime = _TEXT_MIME.get(fp.suffix.lower()) or _IMG_MIME.get(fp.suffix.lower()) or \
           _DOWNLOAD_MIME.get(fp.suffix.lower(), "application/octet-stream")
    return FileResponse(fp, media_type=mime, filename=fp.name)


@app.get("/api/books/{name}/cover")
def api_cover(name: str):
    """尝试返回 EPUB 封面图片"""
    fp = books.resolve_file(name, "images/000001.png")
    if fp is None:
        raise HTTPException(404, "无封面")
    return FileResponse(fp, media_type="image/png")


# ===================== P3a: 上传 + 任务队列 =====================

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), title: str = Form(""),
                     target_lang: str = Form("zh")):
    """上传书 → 建任务 → 后台线程 convert"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in jobs.ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的类型 {ext or '(无扩展名)'}，仅支持 PDF/DOCX/EPUB")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > jobs.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {jobs.MAX_UPLOAD_BYTES // (1024*1024)}MB 上限")
    t = title.strip() or (Path(file.filename).stem if file.filename else "book")
    job = jobs.create_job(t, ext, target_lang)
    jobs.save_input(job["id"], data)
    jobs.update_job(job["id"], status="converting")
    jobs.write_progress(job["id"], "converting", "开始转换...")
    threading.Thread(target=_convert_worker, args=(job["id"],), daemon=True).start()
    return {"job": jobs.get_job(job["id"])}


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    job["progress"] = jobs.read_progress(job_id)
    return job


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return {"job": job}


# ===================== P3c: SSE 实时进度 =====================

@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    """SSE 实时进度：每 2s 读 progress.json，变化即推；16s 心跳保活；终态推 done。"""
    async def gen():
        last_key = None
        idle = 0
        while True:
            job = jobs.get_job(job_id)
            if job is None:
                payload = {"job_id": job_id, "status": "notfound", "progress": {}}
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield f"event: done\ndata: {json.dumps({'job_id': job_id, 'status': 'notfound'})}\n\n"
                return
            prog = jobs.read_progress(job_id)
            key = (job.get("status"), prog.get("updated_at"), prog.get("chunks_done"),
                   prog.get("chunks_total"), prog.get("message"))
            if key != last_key:
                last_key = key
                payload = {"job_id": job_id, "status": job.get("status"), "progress": prog}
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                idle = 0
            else:
                idle += 1
                if idle % 8 == 0:  # 每 ~16s 心跳，防代理断空闲连接
                    yield ": ping\n\n"
            if job.get("status") in jobs.TERMINAL:
                yield f"event: done\ndata: {json.dumps({'job_id': job_id, 'status': job.get('status')})}\n\n"
                return
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


# ===================== 回收站（藏书 + 任务） =====================

TRASH_ROOT = config.WORK_ROOT / ".trash"
TRASH_BOOKS = TRASH_ROOT / "books"
TRASH_JOBS = TRASH_ROOT / "jobs"


def _trashed_at(p: Path) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(p.stat().st_mtime))
    except Exception:
        return ""


@app.get("/api/trash")
def api_trash():
    """回收站列表：藏书 + 任务"""
    books = []
    if TRASH_BOOKS.is_dir():
        for d in sorted(TRASH_BOOKS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            name = d.name[:-len("_temp")] if d.name.endswith("_temp") else d.name
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
            books.append({"name": name, "title": name, "size": size, "trashed_at": _trashed_at(d)})
    jobs = []
    if TRASH_JOBS.is_dir():
        for d in sorted(TRASH_JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            jp = d / "job.json"
            if not jp.exists():
                continue
            try:
                job = json.loads(jp.read_text(encoding="utf-8"))
            except Exception:
                continue
            jobs.append({"id": job.get("id"), "title": job.get("title"),
                         "status": job.get("status"), "trashed_at": _trashed_at(d)})
    return {"books": books, "jobs": jobs}


@app.post("/api/books/{name}/trash")
def api_book_trash(name: str):
    """书移入回收站；有非终态任务引用时拒绝"""
    src = config.WORK_ROOT / f"{name}_temp"
    if not src.is_dir():
        raise HTTPException(404, "书不存在")
    for jd in jobs.JOBS_ROOT.iterdir():
        if not jd.is_dir():
            continue
        try:
            job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("temp_dir") == f"{name}_temp" and job.get("status") not in jobs.TERMINAL:
            raise HTTPException(409, "该书有进行中的翻译任务，无法删除")
    TRASH_BOOKS.mkdir(parents=True, exist_ok=True)
    dst = TRASH_BOOKS / src.name
    if dst.exists():
        raise HTTPException(409, "回收站已有同名书")
    src.rename(dst)
    return {"ok": True}


@app.post("/api/trash/books/{name}/restore")
def api_trash_book_restore(name: str):
    src = TRASH_BOOKS / f"{name}_temp"
    if not src.is_dir():
        raise HTTPException(404, "回收站无此书")
    dst = config.WORK_ROOT / src.name
    if dst.exists():
        raise HTTPException(409, "书库已有同名书")
    src.rename(dst)
    return {"ok": True}


@app.post("/api/trash/books/empty")
def api_trash_books_empty():
    if TRASH_BOOKS.is_dir():
        for d in TRASH_BOOKS.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            else:
                d.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/trash")
def api_job_trash(job_id: str):
    """任务移入回收站；非终态（进行中）任务拒绝"""
    src = jobs.JOBS_ROOT / job_id
    if not (src / "job.json").exists():
        raise HTTPException(404, "任务不存在")
    try:
        job = json.loads((src / "job.json").read_text(encoding="utf-8"))
    except Exception:
        job = {}
    if job.get("status") not in jobs.TERMINAL:
        raise HTTPException(409, "进行中的任务无法删除")
    TRASH_JOBS.mkdir(parents=True, exist_ok=True)
    dst = TRASH_JOBS / job_id
    if dst.exists():
        raise HTTPException(409, "回收站已有同名任务")
    src.rename(dst)
    return {"ok": True}


@app.post("/api/trash/jobs/{job_id}/restore")
def api_trash_job_restore(job_id: str):
    src = TRASH_JOBS / job_id
    if not (src / "job.json").exists():
        raise HTTPException(404, "回收站无此任务")
    dst = jobs.JOBS_ROOT / job_id
    if dst.exists():
        raise HTTPException(409, "任务队列已有同名任务")
    src.rename(dst)
    return {"ok": True}


@app.post("/api/trash/jobs/empty")
def api_trash_jobs_empty():
    if TRASH_JOBS.is_dir():
        for d in TRASH_JOBS.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            else:
                d.unlink(missing_ok=True)
    return {"ok": True}


# ===================== P4: 术语表编辑 =====================

_GLOSSARY_EMPTY = {"version": 2, "terms": [], "high_frequency_top_n": 20,
                   "applied_meta_hashes": {}}

def _glossary_fp(name: str) -> Path | None:
    """书 name → glossary.json 路径（不存在返回 None 由调用方区分）"""
    b = books.get_book(name)
    if b is None:
        return None
    return (config.WORK_ROOT / b["dir"]) / "glossary.json"


def _load_glossary_mod():
    """加载 glossary.py（校验/规范化用），缓存避免重复 import"""
    if "glossary" not in sys.modules:
        sys.path.insert(0, str(config.SCRIPT_DIR))
    import glossary  # noqa: WPS433
    return glossary


@app.get("/api/books/{name}/glossary")
def api_glossary_get(name: str):
    fp = _glossary_fp(name)
    if fp is None:
        raise HTTPException(404, "书不存在")
    g = dict(_GLOSSARY_EMPTY)
    has = fp.exists()
    if has:
        try:
            g = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(500, "glossary.json 损坏或非法 JSON")
    return {"glossary": g, "has_glossary": has,
            "terms_count": len(g.get("terms", []))}


@app.put("/api/books/{name}/glossary")
def api_glossary_put(name: str, payload: dict):
    fp = _glossary_fp(name)
    if fp is None:
        raise HTTPException(404, "书不存在")
    b = books.get_book(name)
    terms = payload.get("terms")
    if not isinstance(terms, list):
        raise HTTPException(400, "terms 必须是数组")
    g = {
        "version": 2,
        "terms": terms,
        "high_frequency_top_n": payload.get("high_frequency_top_n", 20),
        "applied_meta_hashes": payload.get("applied_meta_hashes", {}),
    }
    gm = _load_glossary_mod()
    # 宽容处理：为缺省字段补 v2 默认值（gender/aliases/confidence 等），再走严格校验
    for t in terms:
        if isinstance(t, dict):
            gm._v2_term_defaults(t)
    # 校验（load_glossary 抛 ValueError）→ 规范化原子写（save_glossary）
    try:
        data = gm.load_glossary_for_payload(g) if hasattr(gm, "load_glossary_for_payload") else None
    except ValueError as e:
        raise HTTPException(400, f"术语表校验失败：{e}")
    # 无专用函数则写临时文件走 load_glossary 校验
    if data is None:
        tmp = fp.with_name(".glossary-incoming.json")
        try:
            tmp.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
            data = gm.load_glossary(str(tmp))
        except ValueError as e:
            raise HTTPException(400, f"术语表校验失败：{e}")
        finally:
            tmp.unlink(missing_ok=True)
    try:
        gm.save_glossary(str(fp), data)  # 补默认字段 + 再校验 + 原子写
    except ValueError as e:
        raise HTTPException(400, f"术语表校验失败：{e}")

    # 若书已完成/失败/取消，重置对应 job 为 ready 触发按新术语重译
    affected = _count_affected_chunks(str(config.WORK_ROOT / b["dir"]))
    job_id = _job_for_book(name)
    if job_id:
        job = jobs.get_job(job_id)
        if job and job.get("status") in ("done", "failed", "cancelled"):
            jobs.update_job(job_id, status="ready", error=None)
            jobs.write_progress(job_id, "ready",
                                f"术语表已更新，将按新术语重译约 {affected} 块...")
    return {"ok": True, "terms_count": len(data.get("terms", [])),
            "affected_chunks": affected}


def _job_for_book(name: str) -> str | None:
    """找 temp_dir == <name>_temp 的 job id"""
    if not jobs.JOBS_ROOT.is_dir():
        return None
    for d in jobs.JOBS_ROOT.iterdir():
        if not d.is_dir():
            continue
        job = jobs.get_job(d.name)
        if job and job.get("temp_dir") == f"{name}_temp":
            return job["id"]
    return None


def _count_affected_chunks(temp_dir: str) -> int:
    """用 run_state plan 数本批需译 chunk 数（术语表变化触发）"""
    try:
        r = subprocess.run(
            [config.PYTHON, str(config.SCRIPT_DIR / "run_state.py"), "plan", temp_dir],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            plan = json.loads(r.stdout)
            return len(plan.get("translation_chunk_ids", []))
    except Exception:
        pass
    return 0


# ===================== P5: 全文搜索 =====================

@app.get("/api/search")
def api_search(q: str = Query(""), scope: str = Query("trans"), limit: int = Query(10)):
    """跨书全文搜索：scope=trans 搜译文(output_chunk) / src 搜原文(chunk) / all 两者"""
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(400, "关键词至少 2 个字符")
    if scope not in ("trans", "src", "all"):
        scope = "trans"
    results = []
    for d in sorted(config.WORK_ROOT.glob("*_temp"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        patterns = []
        if scope in ("trans", "all"):
            patterns.append("output_chunk*.md")
        if scope in ("src", "all"):
            patterns.append("chunk[0-9]*.md")
        hits = []
        total = 0
        for pat in patterns:
            for fp in sorted(d.glob(pat)):
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                idx = text.find(q)
                if idx < 0:
                    continue
                total += 1
                if len(hits) < limit:
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(q) + 60)
                    snippet = text[start:end].replace("\n", " ").strip()
                    hits.append({"chunk": fp.stem, "snippet": snippet})
        if hits:
            cfg = books._read_config(d)
            results.append({
                "book": d.name[:-len("_temp")],
                "title": cfg.get("original_title", d.name[:-len("_temp")]),
                "total": total,
                "hits": hits,
            })
    return {"q": q, "scope": scope, "count": len(results), "results": results}


def _convert_worker(job_id: str):
    """后台线程：调 convert.py 转 markdown chunks，状态 converting→ready/failed"""
    try:
        inp = jobs.input_path(job_id)
        job = jobs.get_job(job_id)
        if inp is None or job is None:
            return
        before = {p.name for p in config.WORK_ROOT.glob("*_temp") if p.is_dir()}
        cmd = [config.PYTHON, str(config.SCRIPT_DIR / "convert.py"), str(inp),
               "--olang", job["target_lang"], "--temp-root", str(config.WORK_ROOT)]
        jobs.write_progress(job_id, "converting", "正在转换（Calibre 解析 + 分块）...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "")[-800:]
            jobs.update_job(job_id, status="failed", error=err)
            jobs.write_progress(job_id, "failed", "转换失败")
            return
        after = {p.name for p in config.WORK_ROOT.glob("*_temp") if p.is_dir()}
        new = after - before
        if not new:
            jobs.update_job(job_id, status="failed", error="转换完成但未找到输出目录")
            jobs.write_progress(job_id, "failed", "转换异常：无输出目录")
            return
        temp_dir = sorted(new)[-1]
        jobs.update_job(job_id, status="ready", temp_dir=temp_dir)
        jobs.write_progress(job_id, "ready", "转换完成，等待翻译")
    except Exception as e:  # noqa: BLE001
        jobs.update_job(job_id, status="failed", error=str(e)[-800:])
        jobs.write_progress(job_id, "failed", "转换异常")


# ===================== P3b: merge watcher（Web 后台线程执行 merge）=====================

_MERGE_MIN_AVAILABLE_MB = 1536  # merge 前内存余量下限


def _merge_watcher():
    """每 60s 扫 merging + merge_request.json 的任务，调 merge_and_build.py 执行合并。

    注意：不用 --cleanup —— 查看器逐块对照依赖 chunk*.md / output_chunk*.md。
    """
    while True:
        try:
            _merge_one_round()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def _merge_one_round():
    if not jobs.JOBS_ROOT.is_dir():
        return
    for d in sorted(jobs.JOBS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        req_fp = d / "merge_request.json"
        job = jobs.get_job(d.name)
        if job is None or job.get("status") != "merging" or not req_fp.exists():
            continue
        try:
            req = json.loads(req_fp.read_text(encoding="utf-8"))
        except Exception:
            req = {}
        title = req.get("title") or ""
        temp_dir = job.get("temp_dir")
        if not temp_dir:
            jobs.update_job(d.name, status="failed", error="temp_dir 为空")
            continue
        # 内存余量检查：不足则等下一轮
        avail_mb = 0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass
        if avail_mb and avail_mb < _MERGE_MIN_AVAILABLE_MB:
            jobs.write_progress(d.name, "merging", f"内存不足({avail_mb}MB)，等待下一轮", 0, None)
            continue
        jobs.write_progress(d.name, "merging", "合并构建中（生成 HTML/DOCX/EPUB/PDF）...")
        cmd = [config.PYTHON, str(config.SCRIPT_DIR / "merge_and_build.py"),
               "--temp-dir", str(config.WORK_ROOT / temp_dir), "--title", title]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0:
            jobs.update_job(d.name, status="done")
            jobs.write_progress(d.name, "done", "翻译与合并全部完成",
                                done=job.get("chunk_count") or 0,
                                total=job.get("chunk_count") or 0)
        else:
            err = (r.stderr or r.stdout or "")[-800:]
            jobs.update_job(d.name, status="failed", error=err)
            jobs.write_progress(d.name, "failed", "合并构建失败")
            req_fp.unlink(missing_ok=True)


@app.on_event("startup")
def _start_merge_watcher():
    threading.Thread(target=_merge_watcher, daemon=True).start()


def main():
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8788, log_level="info")


if __name__ == "__main__":
    main()
