"""Web server: upload page, job status and download."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .jobs import JobManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="PDF 说人话翻译")
manager = JobManager()


def _job_or_404(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "找不到这个任务")
    return job


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    mode = os.environ.get("REWRITER", "claude")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {"mode": mode, "api_key_configured": has_key}


@app.post("/api/jobs")
def create_job(
    file: UploadFile = File(...),
    target_lang: str = Form("zh"),
    first_page: int | None = Form(None),
    last_page: int | None = Form(None),
):
    if target_lang not in ("zh", "en"):
        raise HTTPException(400, "目标语言只能是 zh 或 en")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 PDF 文件")
    job = manager.create(file.filename, target_lang, first_page, last_page)
    with job.input.open("wb") as out:  # stream to disk; files can be hundreds of MB
        shutil.copyfileobj(file.file, out, length=8 * 1024 * 1024)
    manager.submit(job)
    return job.read()


@app.get("/api/jobs")
def list_jobs():
    return manager.list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id).read()


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = _job_or_404(job_id)
    if job.read()["state"] in ("queued", "running", "assembling"):
        raise HTTPException(409, "任务还在进行中")
    manager.retry(job)
    return job.read()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _job_or_404(job_id)
    manager.cancel(job)
    return job.read()


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = _job_or_404(job_id)
    status = job.read()
    if status["state"] != "done" or not job.output.exists():
        raise HTTPException(404, "还没有生成完")
    stem = Path(status["filename"]).stem
    suffix = "说人话版" if status["target_lang"] == "zh" else "plain-English"
    return FileResponse(job.output, media_type="application/pdf", filename=f"{stem}-{suffix}.pdf")
