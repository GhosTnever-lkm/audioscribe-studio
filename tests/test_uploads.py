import tempfile
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

    def test_product_limit_is_exactly_five_decimal_gigabytes(self):
        from audioscribe.config import MAX_UPLOAD_BYTES
        self.assertEqual(MAX_UPLOAD_BYTES, 5_000_000_000)


if __name__ == "__main__":
    unittest.main()
