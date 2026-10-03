"""Background jobs: split a PDF into pages, rewrite each page, then rebuild the PDF.

Every finished page is saved to disk straight away, so a job that stops part-way (crash,
restart, network loss) carries on from where it stopped and never pays for a page twice.
PyMuPDF documents are only touched from the job thread; the worker pool only calls Claude.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import pymupdf

from .pdf_layout import TextBlock, build_output, extract_page
from .rewriter import Usage, make_rewriter

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA_DIR / "jobs"
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
MAX_IN_FLIGHT = CONCURRENCY * 3

ACTIVE = {"queued", "running", "assembling"}


class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = JOBS_DIR / job_id
        self.pages_dir = self.dir / "pages"
        self.input = self.dir / "input.pdf"
        self.output = self.dir / "output.pdf"
        self.status_path = self.dir / "status.json"
        self._lock = threading.Lock()

    def read(self) -> dict:
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def update(self, **changes) -> dict:
        with self._lock:
            status = self.read() if self.status_path.exists() else {}
            status.update(changes, updated_at=time.time())
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.status_path)
            return status

    def page_path(self, index: int) -> Path:
        return self.pages_dir / f"{index + 1:05d}.json"


class JobManager:
    def __init__(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[str] = queue.Queue()
        self.cancelled: set[str] = set()
        self._rewriter = None
        threading.Thread(target=self._run_forever, daemon=True).start()
        self._resume_unfinished()

    @property
    def rewriter(self):
        if self._rewriter is None:
            self._rewriter = make_rewriter()
        return self._rewriter

    # ---- public API -------------------------------------------------------------------

    def create(self, filename: str, target_lang: str, first_page: int | None, last_page: int | None) -> Job:
        job = Job(uuid.uuid4().hex[:12])
        job.pages_dir.mkdir(parents=True)
        job.update(
            id=job.id,
            filename=filename,
            target_lang=target_lang,
            first_page=first_page,
            last_page=last_page,
            state="uploading",
            total_pages=0,
            done_pages=0,
            failed_pages=[],
            usage=Usage().__dict__,
            cost_usd=0.0,
            error=None,
            created_at=time.time(),
        )
        return job

    def submit(self, job: Job) -> None:
        job.update(state="queued")
        self.queue.put(job.id)

    def get(self, job_id: str) -> Job | None:
        if not job_id.isalnum():
            return None
        job = Job(job_id)
        return job if job.status_path.exists() else None

    def list(self) -> list[dict]:
        jobs = []
        for d in JOBS_DIR.iterdir():
            job = Job(d.name)
            if job.status_path.exists():
                jobs.append(job.read())
        return sorted(jobs, key=lambda s: s.get("created_at", 0), reverse=True)

    def retry(self, job: Job) -> None:
        """Run the job again; pages already done are skipped."""
        self.cancelled.discard(job.id)
        job.update(failed_pages=[], error=None)
        self.submit(job)

    def cancel(self, job: Job) -> None:
        self.cancelled.add(job.id)
        if job.read()["state"] == "queued":
            job.update(state="cancelled")

    # ---- worker -----------------------------------------------------------------------

    def _resume_unfinished(self) -> None:
        for status in sorted(self.list(), key=lambda s: s.get("created_at", 0)):
            if status["state"] in ACTIVE:
                log.info("resuming job %s", status["id"])
                self.queue.put(status["id"])

    def _run_forever(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.get(job_id)
            if job is None or job.read()["state"] == "cancelled":
                continue
            try:
                self._run(job)
            except Exception as e:  # keep the worker alive for the next job
                log.exception("job %s failed", job_id)
                job.update(state="failed", error=str(e))

    def _page_range(self, status: dict, page_count: int) -> range:
        first = max(1, status.get("first_page") or 1)
        last = min(page_count, status.get("last_page") or page_count)
        return range(first - 1, last)

    def _run(self, job: Job) -> None:
        status = job.update(state="running", error=None, failed_pages=[])
        target_lang = status["target_lang"]
        usage = Usage(**status.get("usage", {}))
        failed: list[int] = []

        doc = pymupdf.open(job.input)
        pages = self._page_range(status, doc.page_count)
        done = sum(1 for i in pages if job.page_path(i).exists())
        job.update(total_pages=len(pages), done_pages=done)

        # Build the client here, not lazily inside the pool: constructing it from several
        # threads at once races inside the SDK.
        rewriter = self.rewriter

        def rewrite(index: int, kind: str, blocks: list[TextBlock]) -> tuple[int, dict, Usage]:
            request = [{"id": b.id, "text": b.text} for b in blocks]
            result = rewriter.rewrite(request, target_lang, index + 1)
            record = {
                "kind": kind,
                "blocks": [b.to_dict() for b in blocks],
                "replacements": {str(k): v for k, v in result.texts.items()},
            }
            return index, record, result.usage

        in_flight = {}

        def collect(block_until_one: bool) -> None:
            nonlocal done
            if not in_flight:
                return
            finished, _ = wait(in_flight, timeout=None if block_until_one else 0, return_when=FIRST_COMPLETED)
            for fut in finished:
                index = in_flight.pop(fut)
                try:
                    _, record, page_usage = fut.result()
                except Exception as e:
                    log.warning("page %d failed: %s", index + 1, e)
                    failed.append(index + 1)
                    job.update(failed_pages=sorted(failed))
                    continue
                job.page_path(index).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                usage.add(page_usage)
                done += 1
                job.update(done_pages=done, usage=usage.__dict__, cost_usd=round(usage.cost_usd, 4))

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for index in pages:
                if job.id in self.cancelled:
                    break
                if job.page_path(index).exists():
                    continue
                page_text = extract_page(doc[index])
                if not page_text.blocks:
                    # Nothing to rewrite (blank, picture-only or unreadable scan): keep as is.
                    record = {"kind": page_text.kind, "blocks": [], "replacements": {}}
                    job.page_path(index).write_text(json.dumps(record), encoding="utf-8")
                    done += 1
                    job.update(done_pages=done)
                    continue
                in_flight[pool.submit(rewrite, index, page_text.kind, page_text.blocks)] = index
                while len(in_flight) >= MAX_IN_FLIGHT:
                    collect(block_until_one=True)
                collect(block_until_one=False)
            while in_flight:
                collect(block_until_one=True)

        if job.id in self.cancelled:
            self.cancelled.discard(job.id)
            doc.close()
            job.update(state="cancelled")
            return

        doc.close()
        job.update(state="assembling")

        def load_record(index: int):
            path = job.page_path(index)
            if not path.exists():
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            blocks = [TextBlock.from_dict(b) for b in record["blocks"]]
            replacements = {int(k): v for k, v in record["replacements"].items()}
            return record["kind"], blocks, replacements

        tmp = job.output.with_suffix(".tmp.pdf")
        build_output(job.input, pages, load_record, tmp)
        tmp.replace(job.output)
        job.update(
            state="done",
            failed_pages=sorted(failed),
            error=(f"{len(failed)} 页翻译失败，已保留原文，可点“重试失败页”" if failed else None),
        )
