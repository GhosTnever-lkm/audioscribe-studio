import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from audioscribe.config import Settings
from audioscribe.database import Database
from audioscribe.worker import TranscriptionWorker


class FakeTranscriber:
    def __init__(self, cancel_after_first=None):
        self.calls = []
        self.cancel_after_first = cancel_after_first

    def transcribe(self, pcm, language):
        self.calls.append(len(pcm))
        if len(self.calls) == 1 and self.cancel_after_first:
            self.cancel_after_first.set()
        return [{"start": 0.1, "end": 0.4, "text": f"фрагмент-{len(self.calls)}"}]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(data_dir=root, segment_seconds=1, overlap_seconds=0)
        self.db = Database(root / "db.sqlite3")
        self.worker = TranscriptionWorker(self.settings, self.db)
        self.db.add_upload("up", "sample.wav", str(root / "sample.part"), 64000)
        (root / "audio").mkdir(exist_ok=True)
        self.source = root / "audio" / "sample.wav"
        self.source.write_bytes(b"test")
        self.db.create_job("job", "up", "sample.wav", str(self.source), "tiny", None)
        one_second = np.arange(16_000, dtype="<i2").tobytes()
        self.pcm = one_second + one_second

    def tearDown(self):
        self.tmp.cleanup()

    def run_worker(self, transcriber, cancel=None):
        def reader(_path, chunk_seconds):
            size = chunk_seconds * 16_000 * 2
            for index, offset in enumerate(range(0, len(self.pcm), size)):
                yield index, float(index * chunk_seconds), self.pcm[offset:offset + size]

        self.worker.process(self.db.get_job("job"), transcriber, cancel, duration_fn=lambda _path: 2.0, reader=reader)

    def test_decodes_in_bounded_chunks_and_stores_absolute_timestamps(self):
        model = FakeTranscriber()
        self.run_worker(model)
        self.assertEqual(model.calls, [16_000, 16_000])
        segments = self.db.get_segments("job")
        self.assertAlmostEqual(segments[0]["start"], 0.1)
        self.assertAlmostEqual(segments[1]["start"], 1.1)
        self.assertEqual(self.db.get_job("job")["status"], "completed")

    def test_pause_checkpoints_then_resumes_without_reprocessing_saved_chunk(self):
        cancel = threading.Event()
        first_run = FakeTranscriber(cancel)
        self.run_worker(first_run, cancel)
        self.assertEqual(len(first_run.calls), 1)
        self.assertEqual(self.db.completed_chunks("job"), {0})
        self.assertEqual(self.db.get_job("job")["status"], "paused")
        second_run = FakeTranscriber()
        self.run_worker(second_run)
        self.assertEqual(len(second_run.calls), 1)
        self.assertEqual(len(self.db.get_segments("job")), 2)
        self.assertEqual(self.db.get_job("job")["status"], "completed")

    def test_model_input_is_numeric_float_data(self):
        class InspectingTranscriber:
            def transcribe(self, pcm, _language):
                self.assertion(pcm)
                return []

            @staticmethod
            def assertion(pcm):
                assert pcm.dtype == np.float32
                assert pcm.size <= 16_000

        self.run_worker(InspectingTranscriber())

    def test_boundary_spanning_phrase_is_saved_once_by_midpoint(self):
        class BoundaryTranscriber:
            def __init__(self):
                self.calls = 0

            def transcribe(self, _pcm, _language):
                self.calls += 1
                if self.calls == 1:
                    return [{"start": 0.8, "end": 1.2, "text": "стык"}]
                return [{"start": 0.0, "end": 0.2, "text": "стык"}]

        model = BoundaryTranscriber()
        self.run_worker(model)
        segments = self.db.get_segments("job")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "стык")


if __name__ == "__main__":
    unittest.main()
