from __future__ import annotations

import gc
import threading
from pathlib import Path
from typing import Callable, Iterator

from .config import Settings
from .database import Database

MODEL_CHOICES = {"tiny", "base", "small", "medium", "large-v3"}
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2


def probe_duration(path: Path) -> float:
    """Read duration metadata without decoding or buffering the complete recording."""
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("Не установлен аудиодекодер PyAV. Установи extras [transcription].") from exc
    try:
        with av.open(str(path)) as container:
            audio_streams = container.streams.audio
            if not audio_streams:
                raise RuntimeError("В аудиофайле не найдена звуковая дорожка.")
            if container.duration is not None:
                duration = float(container.duration / av.time_base)
            else:
                stream = audio_streams[0]
                duration = float(stream.duration * stream.time_base) if stream.duration is not None else 0.0
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("PyAV не смог прочитать аудиофайл. Проверь формат и целостность записи.") from exc
    if duration <= 0:
        raise RuntimeError("В файле не удалось определить длительность аудиозаписи.")
    return duration


def iter_pcm_chunks(path: Path, chunk_seconds: int, *, sample_rate: int = SAMPLE_RATE) -> Iterator[tuple[int, float, bytes]]:
    """Yield bounded mono s16 PCM windows decoded sequentially from the source file."""
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("Не установлен аудиодекодер PyAV. Установи extras [transcription].") from exc
    samples_per_chunk = int(round(chunk_seconds * sample_rate))
    chunk_bytes = samples_per_chunk * BYTES_PER_SAMPLE
    pending = bytearray()
    index = 0
    try:
        with av.open(str(path)) as container:
            streams = container.streams.audio
            if not streams:
                raise RuntimeError("В аудиофайле не найдена звуковая дорожка.")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
            for frame in container.decode(streams[0]):
                for converted in resampler.resample(frame):
                    pending.extend(converted.to_ndarray().reshape(-1).tobytes())
                    while len(pending) >= chunk_bytes:
                        yield index, index * samples_per_chunk / sample_rate, bytes(pending[:chunk_bytes])
                        del pending[:chunk_bytes]
                        index += 1
            for converted in resampler.resample(None):
                pending.extend(converted.to_ndarray().reshape(-1).tobytes())
                while len(pending) >= chunk_bytes:
                    yield index, index * samples_per_chunk / sample_rate, bytes(pending[:chunk_bytes])
                    del pending[:chunk_bytes]
                    index += 1
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("PyAV не смог декодировать аудиофайл. Проверь формат и целостность записи.") from exc
    if pending:
        yield index, index * samples_per_chunk / sample_rate, bytes(pending)


class FasterWhisperTranscriber:
    """Loads the optional local model only when the first job starts."""

    def __init__(self, model_name: str):
        if model_name not in MODEL_CHOICES:
            raise ValueError("Неизвестный размер модели.")
        self.model_name = model_name
        self._model = None

    def transcribe(self, pcm, language: str | None) -> list[dict]:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("Не установлен модуль транскрипции. Установи extras [transcription].") from exc
            # CPU int8 works predictably without requiring a matching CUDA/cuDNN setup.
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        segments, _info = self._model.transcribe(pcm, language=language, vad_filter=True, beam_size=5)
        return [{"start": float(s.start), "end": float(s.end), "text": str(s.text).strip()} for s in segments if str(s.text).strip()]


class TranscriptionWorker:
    """One local worker; decodes fixed-size windows and checkpoints each finished window."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._model_name: str | None = None
        self._transcriber: FasterWhisperTranscriber | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audioscribe-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def pause(self, job_id: str) -> bool:
        with self._lock:
            event = self._active.get(job_id)
        if not event:
            return False
        event.set()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self.db.claim_job()
            if not job_id:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            job = self.db.get_job(job_id)
            cancel = threading.Event()
            with self._lock:
                self._active[job_id] = cancel
            try:
                if self._model_name != job["model"]:
                    self._transcriber = None
                    gc.collect()
                    self._transcriber = FasterWhisperTranscriber(job["model"])
                    self._model_name = job["model"]
                self.process(job, self._transcriber, cancel)
            except Exception as exc:
                self.db.set_job_state(job_id, "failed", f"{type(exc).__name__}: {exc}"[:1000])
            finally:
                with self._lock:
                    self._active.pop(job_id, None)

    def process(
        self,
        job: dict,
        transcriber,
        cancel: threading.Event | None = None,
        *,
        duration_fn: Callable[[Path], float] = probe_duration,
        reader: Callable[[Path, int], Iterator[tuple[int, float, bytes]]] = iter_pcm_chunks,
    ) -> None:
        """Public for deterministic tests; chunk results and completion markers commit together."""
        cancel = cancel or threading.Event()
        path = Path(job["audio_path"])
        duration = duration_fn(path)
        self.db.set_duration(job["id"], duration)
        chunk_sec = self.settings.segment_seconds
        overlap_bytes = int(self.settings.overlap_seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE
        completed = self.db.completed_chunks(job["id"])
        resume_index = 0
        while resume_index in completed:
            resume_index += 1

        # Re-decode from the beginning when resuming so all container formats have reliable
        # timestamps. Previously completed windows skip inference and rebuild the overlap tail.
        previous_tail = b""
        saw_window = False
        for index, window_start, current in reader(path, chunk_sec):
            if self._stop.is_set() or cancel.is_set():
                self.db.set_job_state(job["id"], "paused")
                return
            saw_window = True
            if index < resume_index:
                previous_tail = current[-overlap_bytes:] if overlap_bytes else b""
                continue
            window = previous_tail + current if index > 0 else current
            absolute_window_start = window_start - (len(previous_tail) / (SAMPLE_RATE * BYTES_PER_SAMPLE))
            try:
                import numpy as np
            except ImportError as exc:
                raise RuntimeError("Для передачи PCM в faster-whisper нужен NumPy; он устанавливается вместе с ним.") from exc
            pcm = np.frombuffer(window, dtype="<i2").astype(np.float32) / 32768.0
            raw = transcriber.transcribe(pcm, job["language"])
            owned_start = window_start
            owned_end = min(duration, window_start + (len(current) / (SAMPLE_RATE * BYTES_PER_SAMPLE)))
            absolute = []
            for segment in raw:
                start = absolute_window_start + float(segment["start"])
                end = absolute_window_start + float(segment["end"])
                # A phrase belongs to the window containing its midpoint. The same rule applies
                # to the first window, so a phrase crossing a boundary is never stored twice.
                midpoint = (start + end) / 2
                if owned_start <= midpoint < owned_end:
                    absolute.append({"start": max(0.0, start), "end": max(0.0, end), "text": str(segment["text"])})
            self.db.save_chunk(job["id"], index, owned_start, owned_end, absolute)
            self.db.set_progress(job["id"], min(1.0, owned_end / duration))
            previous_tail = current[-overlap_bytes:] if overlap_bytes else b""
        if not saw_window and duration > 0:
            raise RuntimeError("Аудиодорожка пуста или не содержит декодируемых звуковых данных.")
        if self._stop.is_set() or cancel.is_set():
            self.db.set_job_state(job["id"], "paused")
        else:
            self.db.set_progress(job["id"], 1.0)
            self.db.set_job_state(job["id"], "completed")

