import concurrent.futures
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from audioscribe.config import Settings
from audioscribe.database import Database
from audioscribe.uploads import UploadError, UploadService, clean_filename


class UploadServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), max_upload_bytes=16, upload_chunk_bytes=4)
        self.db = Database(Path(self.tmp.name) / "db.sqlite3")
        self.service = UploadService(self.settings, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleans_path_segments_from_filename(self):
        self.assertEqual(clean_filename("..\\secret\\meeting.wav"), "meeting.wav")

    def test_rejects_over_limit_before_creating_file(self):
        with self.assertRaises(UploadError) as caught:
            self.service.create("big.wav", 17)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(list((Path(self.tmp.name) / "uploads").glob("*.part")), [])

    def test_upload_offsets_and_completion(self):
        session = self.service.create("meeting.wav", 8)
        self.assertEqual(self.service.append(session["id"], 0, [b"1234"]), 4)
        with self.assertRaises(UploadError) as caught:
            self.service.append(session["id"], 0, [b"5678"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.service.append(session["id"], 4, [b"5678"]), 8)
        job = self.service.complete(session["id"], "small", "ru")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(Path(job["audio_path"]).read_bytes(), b"12345678")
        self.assertEqual(job["language"], "ru")

    def test_cancel_removes_unfinished_upload_and_file(self):
        session = self.service.create("unfinished.wav", 8)
        self.service.append(session["id"], 0, [b"part"])
        path = Path(self.db.get_upload(session["id"])["path"])

        self.service.cancel(session["id"])

        self.assertIsNone(self.db.get_upload(session["id"]))
        self.assertFalse(path.exists())

    def test_cancel_rejects_upload_that_became_a_job(self):
        session = self.service.create("complete.wav", 4)
        self.service.append(session["id"], 0, [b"data"])
        job = self.service.complete(session["id"])

        with self.assertRaises(UploadError) as caught:
            self.service.cancel(session["id"])

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.get_job(job["id"])["status"], "queued")
        self.assertEqual(Path(job["audio_path"]).read_bytes(), b"data")

    def test_cancel_waits_for_in_flight_append(self):
        session = self.service.create("race.wav", 4)
        upload_path = Path(self.db.get_upload(session["id"])["path"])
        append_started = threading.Event()
        allow_append = threading.Event()
        cancel_waiting_for_lock = threading.Event()
        real_lock = self.service._append_lock

        class ObservedLock:
            def __enter__(self):
                if getattr(threading.current_thread(), "testing_upload_cancel", False):
                    cancel_waiting_for_lock.set()
                real_lock.acquire()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        self.service._append_lock = ObservedLock()

        def slow_chunks():
            append_started.set()
            if not allow_append.wait(5):
                raise RuntimeError("test timed out waiting to finish the chunk")
            yield b"data"

        def cancel():
            threading.current_thread().testing_upload_cancel = True
            try:
                self.service.cancel(session["id"])
            finally:
                del threading.current_thread().testing_upload_cancel

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            append_future = pool.submit(self.service.append, session["id"], 0, slow_chunks())
            self.assertTrue(append_started.wait(2))
            cancel_future = pool.submit(cancel)
            try:
                self.assertTrue(cancel_waiting_for_lock.wait(2))
                self.assertFalse(cancel_future.done())
            finally:
                allow_append.set()

            self.assertEqual(append_future.result(timeout=3), 4)
            cancel_future.result(timeout=3)

        self.assertIsNone(self.db.get_upload(session["id"]))
        self.assertFalse(upload_path.exists())

    def test_rejects_incomplete_upload(self):
        session = self.service.create("unfinished.mp3", 8)
        self.service.append(session["id"], 0, [b"1234"])
        with self.assertRaises(UploadError) as caught:
            self.service.complete(session["id"], "small", None)
        self.assertEqual(caught.exception.status_code, 409)

    def test_overrun_rolls_back_to_last_acknowledged_offset(self):
        session = self.service.create("a.wav", 8)
        with self.assertRaises(UploadError):
            self.service.append(session["id"], 0, [b"123456789"])
        self.assertEqual(self.db.get_upload(session["id"])["received_bytes"], 0)
        self.assertEqual(Path(self.db.get_upload(session["id"])["path"]).stat().st_size, 0)

    def test_unacknowledged_tail_from_crash_is_truncated_before_retry(self):
        session = self.service.create("retry.wav", 8)
        path = Path(self.db.get_upload(session["id"])["path"])
        path.write_bytes(b"lost-ack")
        self.assertEqual(self.service.append(session["id"], 0, [b"12345678"]), 8)
        self.assertEqual(path.read_bytes(), b"12345678")

    def test_simultaneous_retries_at_same_offset_do_not_append_twice(self):
        session = self.service.create("retry.wav", 4)
        start = threading.Barrier(3)

        def send_chunk():
            start.wait()
            try:
                return self.service.append(session["id"], 0, [b"data"])
            except UploadError as exc:
                return exc.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send_chunk)
            second = pool.submit(send_chunk)
            start.wait()
            results = [first.result(), second.result()]

        self.assertCountEqual(results, [4, 409])
        upload = self.db.get_upload(session["id"])
        self.assertEqual(upload["received_bytes"], 4)
        self.assertEqual(Path(upload["path"]).read_bytes(), b"data")

    def test_product_limit_is_exactly_five_decimal_gigabytes(self):
        from audioscribe.config import MAX_UPLOAD_BYTES
        self.assertEqual(MAX_UPLOAD_BYTES, 5_000_000_000)

    def test_verifies_uploaded_ranges_against_the_selected_file(self):
        session = self.service.create("same-name.wav", 8)
        self.service.append(session["id"], 0, [b"same"])
        self.assertTrue(self.service.verify_chunk(session["id"], 0, 4, hashlib.sha256(b"same").hexdigest()))
        self.assertFalse(self.service.verify_chunk(session["id"], 0, 4, hashlib.sha256(b"diff").hexdigest()))

    def test_rejects_verification_outside_acknowledged_upload(self):
        session = self.service.create("partial.wav", 8)
        self.service.append(session["id"], 0, [b"part"])
        with self.assertRaises(UploadError) as caught:
            self.service.verify_chunk(session["id"], 2, 4, hashlib.sha256(b"rtxx").hexdigest())
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
