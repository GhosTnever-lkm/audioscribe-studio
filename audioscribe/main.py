from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .database import Database
from .uploads import UploadError, UploadService
from .worker import MODEL_CHOICES, TranscriptionWorker


class CreateUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0)
    model: str = "small"
    language: str | None = Field(default=None, max_length=8, pattern=r"^[a-zA-Z]{2,3}$")


class VerifyUploadChunk(BaseModel):
    offset: int = Field(ge=0)
    length: int = Field(gt=0, le=32 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / "audioscribe.sqlite3")
    uploads = UploadService(settings, db)
    worker = TranscriptionWorker(settings, db)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="AudioScribe Studio", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.uploads, app.state.worker = settings, db, uploads, worker

    @app.middleware("http")
    async def same_origin_mutations(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != request.headers.get("host", "").lower() or parsed.scheme not in {"http", "https"}:
                    return JSONResponse({"detail": "Запрос отклонён: внешний источник."}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home():
        page = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        return page.replace("__VERSION__", __version__)

    @app.get("/static/{filename}")
    def static_file(filename: str):
        if filename not in {"app.js", "styles.css", "icon.svg"}:
            raise HTTPException(404, "Файл не найден.")
        return FileResponse(Path(__file__).parent / "static" / filename)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "max_upload_bytes": settings.max_upload_bytes, "chunk_bytes": settings.upload_chunk_bytes}

    @app.post("/api/uploads", status_code=201)
    def create_upload(payload: CreateUpload):
        if payload.model not in MODEL_CHOICES:
            raise HTTPException(422, "Выбери поддерживаемую модель.")
        try:
            session = uploads.create(payload.filename, payload.size, payload.model, payload.language.lower() if payload.language else None)
        except UploadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return session

    @app.head("/api/uploads/{upload_id}")
    def upload_head(upload_id: str):
        session = db.get_upload(upload_id)
        if not session:
            raise HTTPException(404, "Сессия загрузки не найдена.")
        return Response(status_code=204, headers={"Upload-Offset": str(session["received_bytes"]), "Upload-Length": str(session["total_bytes"]), "Cache-Control": "no-store"})

    @app.post("/api/uploads/{upload_id}/verify")
    def verify_upload_chunk(upload_id: str, payload: VerifyUploadChunk):
        try:
            matches = uploads.verify_chunk(upload_id, payload.offset, payload.length, payload.sha256)
        except UploadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return {"matches": matches}

    @app.get("/api/uploads")
    def list_uploads():
        with db.connect() as conn:
            rows = conn.execute("SELECT u.* FROM uploads AS u WHERE NOT EXISTS (SELECT 1 FROM jobs AS j WHERE j.upload_id=u.id) ORDER BY u.created_at DESC").fetchall()
        return [{"id": r["id"], "filename": r["filename"], "size": r["total_bytes"], "offset": r["received_bytes"], "model": r["model"], "language": r["language"]} for r in rows]

    @app.delete("/api/uploads/{upload_id}", status_code=204)
    def cancel_upload(upload_id: str):
        try:
            uploads.cancel(upload_id)
        except UploadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return Response(status_code=204)

    @app.patch("/api/uploads/{upload_id}")
    async def append_upload(upload_id: str, request: Request):
        try:
            session = db.get_upload(upload_id)
            if not session:
                raise UploadError("Сессия загрузки не найдена.", 404)
            length = request.headers.get("content-length")
            if length and int(length) > settings.upload_chunk_bytes:
                raise UploadError("Порция больше допустимого размера.", 413)
            offset = int(request.headers.get("upload-offset", "-1"))
            pieces = []
            body_size = 0
            async for piece in request.stream():
                body_size += len(piece)
                if body_size > settings.upload_chunk_bytes:
                    raise UploadError("Порция больше допустимого размера.", 413)
                pieces.append(piece)
            body = b"".join(pieces)
            new_offset = uploads.append(upload_id, offset, [body])
            return Response(status_code=204, headers={"Upload-Offset": str(new_offset), "Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            if isinstance(exc, UploadError):
                raise HTTPException(exc.status_code, str(exc)) from exc
            raise HTTPException(400, "Некорректный заголовок Upload-Offset.") from exc
        except UploadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.post("/api/uploads/{upload_id}/complete", status_code=202)
    def complete_upload(upload_id: str):
        try:
            job = uploads.complete(upload_id)
        except UploadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        worker.wake()
        return job

    @app.get("/api/jobs")
    def list_jobs():
        return db.list_jobs()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Задание не найдено.")
        return job

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Задание не найдено.")
        if job["status"] == "queued":
            db.set_job_state(job_id, "paused")
            return {"status": "paused"}
        if worker.pause(job_id):
            return {"status": "pausing"}
        raise HTTPException(409, "Это задание сейчас нельзя поставить на паузу.")

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Задание не найдено.")
        if job["status"] not in {"paused", "failed"}:
            raise HTTPException(409, "Продолжить можно только приостановленное или завершившееся с ошибкой задание.")
        db.set_job_state(job_id, "queued")
        worker.wake()
        return {"status": "queued"}

    @app.get("/api/jobs/{job_id}/transcript")
    def transcript(job_id: str, format: str = "txt"):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Задание не найдено.")
        segments = db.get_segments(job_id)
        fmt = format.lower()
        if fmt not in {"txt", "srt", "vtt", "json"}:
            raise HTTPException(422, "Формат должен быть txt, srt, vtt или json.")
        if fmt == "json":
            body, mime = json.dumps({"job": job_id, "filename": job["filename"], "language": job["language"], "duration": job["duration"], "segments": segments}, ensure_ascii=False, indent=2), "application/json; charset=utf-8"
        elif fmt == "txt":
            body, mime = "\n".join(f"[{_clock(s['start'])}] {s['text']}" for s in segments), "text/plain; charset=utf-8"
        else:
            rows = ["WEBVTT", ""] if fmt == "vtt" else []
            for i, seg in enumerate(segments, 1):
                stamp = f"{_subtitle_time(seg['start'], comma=fmt == 'srt')} --> {_subtitle_time(seg['end'], comma=fmt == 'srt')}"
                rows.extend(([str(i)] if fmt == "srt" else []) + [stamp, seg["text"], ""])
            body, mime = "\n".join(rows), "text/vtt; charset=utf-8" if fmt == "vtt" else "application/x-subrip; charset=utf-8"
        return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="transcript.{fmt}"'})

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Задание не найдено.")
        if job["status"] == "running":
            worker.pause(job_id)
            raise HTTPException(409, "Сначала дождись паузы, затем удали задание.")
        deleted = db.delete_job(job_id)
        Path(deleted["audio_path"]).unlink(missing_ok=True)
        return Response(status_code=204)

    return app


def _clock(seconds: float) -> str:
    value = max(0, int(float(seconds)))
    hours, rem = divmod(value, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def _subtitle_time(seconds: float, *, comma: bool) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    sec, ms = divmod(rem, 1000)
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{sec:02d}{separator}{ms:03d}"


app = create_app()


def run() -> None:
    import uvicorn
    settings = Settings.from_env()
    print(f"AudioScribe Studio доступен локально: http://127.0.0.1:{settings.port}")
    uvicorn.run("audioscribe.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()

