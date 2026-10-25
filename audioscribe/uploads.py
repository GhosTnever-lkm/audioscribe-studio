from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from pathlib import Path

from .config import Settings
from .database import Database

SAFE_FILENAME = re.compile(r"[^\w.() -]+", re.UNICODE)


class UploadError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def clean_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    name = SAFE_FILENAME.sub("_", name).strip(" .")
    return (name or "audio-upload")[:180]


class UploadService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.upload_dir = settings.data_dir / "uploads"
        self.jobs_dir = settings.data_dir / "audio"
        # Offset validation and file append must be one atomic operation within
        # this local app process. Otherwise two retried PATCH requests can both
        # observe the same offset and append the same bytes twice.
        self._append_lock = threading.Lock()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, filename: str, size: int, model: str = "small", language: str | None = None) -> dict:
        if size <= 0:
            raise UploadError("Файл пустой или размер не указан.")
        if size > self.settings.max_upload_bytes:
            raise UploadError("Файл больше установленного лимита 5 ГБ.", 413)
        uid = uuid.uuid4().hex
        path = self.upload_dir / f"{uid}.part"
        path.touch()
        safe = clean_filename(filename)
        self.db.add_upload(uid, safe, str(path), size, model, language, filename)
        return {"id": uid, "filename": safe, "size": size, "offset": 0, "model": model, "language": language}

    def append(self, upload_id: str, offset: int, chunks) -> int:
        with self._append_lock:
            return self._append_locked(upload_id, offset, chunks)

    def cancel(self, upload_id: str) -> None:
        """Remove an unfinished upload without racing a chunk append or completion."""
        with self._append_lock:
            with self.db.connect() as conn:
                upload = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
                if not upload:
                    raise UploadError("Сессия загрузки не найдена.", 404)
                if conn.execute("SELECT 1 FROM jobs WHERE upload_id=?", (upload_id,)).fetchone():
                    raise UploadError("Эта загрузка уже стала заданием.", 409)

                try:
                    Path(upload["path"]).unlink(missing_ok=True)
                except OSError as exc:
                    raise UploadError("Не удалось удалить временный файл загрузки.", 500) from exc
                deleted = conn.execute("DELETE FROM uploads WHERE id=?", (upload_id,)).rowcount
                if not deleted:
                    raise UploadError("Сессия загрузки не найдена.", 404)

    def verify_chunk(self, upload_id: str, offset: int, length: int, digest: str) -> bool:
        """Compare one bounded slice of a selected local file with uploaded bytes."""
        with self._append_lock:
            upload = self.db.get_upload(upload_id)
            if not upload:
                raise UploadError("Сессия загрузки не найдена.", 404)
            if length <= 0 or length > self.settings.upload_chunk_bytes:
                raise UploadError("Размер проверяемого блока вне допустимых пределов.")
            expected = int(upload["received_bytes"])
            if offset < 0 or offset + length > expected:
                raise UploadError("Проверяемый блок ещё не подтверждён сервером.", 409)
            path = Path(upload["path"])
            try:
                with path.open("rb") as source:
                    source.seek(offset)
                    block = source.read(length)
            except OSError as exc:
                raise UploadError("Не удалось прочитать уже загруженный блок.", 409) from exc
            if len(block) != length:
                raise UploadError("Файл загрузки не совпадает с сохранённым прогрессом.", 409)
            return hashlib.sha256(block).hexdigest() == digest.lower()

    def _append_locked(self, upload_id: str, offset: int, chunks) -> int:
        upload = self.db.get_upload(upload_id)
        if not upload:
            raise UploadError("Сессия загрузки не найдена.", 404)
        expected = int(upload["received_bytes"])
        if offset != expected:
            raise UploadError(f"Позиция загрузки изменилась: сервер ждёт {expected} байт.", 409)
        path = Path(upload["path"])
        actual = path.stat().st_size if path.exists() else 0
        if actual > expected:
            # A process may have stopped after fsync but before the offset commit.
            # Discard only the unacknowledged tail so the client can safely retry it.
            with path.open("r+b") as target:
                target.truncate(expected)
            actual = expected
        if actual < expected:
            raise UploadError("Файл загрузки не совпадает с сохранённым прогрессом.", 409)
        written = 0
        try:
            with path.open("ab", buffering=0) as target:
                for piece in chunks:
                    if not piece:
                        continue
                    written += len(piece)
                    if expected + written > int(upload["total_bytes"]):
                        raise UploadError("Передано больше данных, чем заявленный размер.", 413)
                    target.write(piece)
                os.fsync(target.fileno())
        except Exception:
            # Keep only acknowledged bytes, so a retry can safely resume at the stored offset.
            with path.open("r+b") as target:
                target.truncate(expected)
            raise
        new_offset = expected + written
        self.db.set_upload_offset(upload_id, new_offset)
        return new_offset

    def complete(self, upload_id: str, model: str | None = None, language: str | None = None) -> dict:
        with self._append_lock:
            return self._complete_locked(upload_id, model, language)

    def _complete_locked(self, upload_id: str, model: str | None = None, language: str | None = None) -> dict:
        upload = self.db.get_upload(upload_id)
        if not upload:
            raise UploadError("Сессия загрузки не найдена.", 404)
        existing_job = self.db.get_job_by_upload(upload_id)
        if existing_job:
            return existing_job
        if upload["received_bytes"] != upload["total_bytes"]:
            raise UploadError("Загрузка ещё не завершена.", 409)
        source = Path(upload["path"])
        job_id = upload_id
        audio_path = self.jobs_dir / f"{job_id}{Path(upload['filename']).suffix.lower()}"
        # If the process ended after moving the file but before committing the job row,
        # this deterministic destination lets the next /complete request recover it.
        if source.is_file() and source.stat().st_size == upload["total_bytes"]:
            source.replace(audio_path)
        if not audio_path.is_file() or audio_path.stat().st_size != upload["total_bytes"]:
            raise UploadError("Размер полученного файла не совпадает с заявленным.", 409)
        self.db.create_job(job_id, upload_id, upload["filename"], str(audio_path), model or upload["model"], upload["language"] if language is None else language)
        return self.db.get_job(job_id)

