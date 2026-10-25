import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx
import numpy as np

from audioscribe.config import MAX_UPLOAD_BYTES, Settings
from audioscribe.main import create_app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=Path(self.tmp.name), max_upload_bytes=32, upload_chunk_bytes=8)
        self.app = create_app(settings, start_worker=False)
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_chunk_upload_returns_offset_and_completes_job(self):
        response = await self.client.post("/api/uploads", json={"filename": "recording.wav", "size": 4, "model": "tiny"})
        self.assertEqual(response.status_code, 201)
        upload_id = response.json()["id"]
        chunk = await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"abcd")
        self.assertEqual(chunk.status_code, 204)
        self.assertEqual(chunk.headers["upload-offset"], "4")
        job = await self.client.post(f"/api/uploads/{upload_id}/complete")
        self.assertEqual(job.status_code, 202)
        self.assertEqual(job.json()["status"], "queued")
        repeated = await self.client.post(f"/api/uploads/{upload_id}/complete")
        self.assertEqual(repeated.json()["id"], job.json()["id"])

    async def test_upload_api_accepts_five_gigabytes_and_rejects_one_byte_over(self):
        settings = Settings(data_dir=Path(self.tmp.name) / "five-gb-limit")
        app = create_app(settings, start_worker=False)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        try:
            health = await client.get("/api/health")
            self.assertEqual(health.json()["max_upload_bytes"], MAX_UPLOAD_BYTES)

            accepted = await client.post(
                "/api/uploads",
                json={"filename": "large-recording.wav", "size": MAX_UPLOAD_BYTES},
            )
            self.assertEqual(accepted.status_code, 201)
            upload = app.state.db.get_upload(accepted.json()["id"])
            self.assertEqual(upload["total_bytes"], MAX_UPLOAD_BYTES)
            self.assertEqual(Path(upload["path"]).stat().st_size, 0)

            rejected = await client.post(
                "/api/uploads",
                json={"filename": "too-large.wav", "size": MAX_UPLOAD_BYTES + 1},
            )
            self.assertEqual(rejected.status_code, 413)
        finally:
            await client.aclose()

    async def test_completion_uses_current_model_and_language_settings(self):
        created = await self.client.post(
            "/api/uploads",
            json={"filename": "recording.wav", "size": 4, "model": "tiny", "language": "en"},
        )
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"data")

        invalid = await self.client.post(
            f"/api/uploads/{upload_id}/complete",
            json={"model": "unknown", "language": "ru"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertIsNone(self.app.state.db.get_job_by_upload(upload_id))

        completed = await self.client.post(
            f"/api/uploads/{upload_id}/complete",
            json={"model": "base", "language": "ru"},
        )

        self.assertEqual(completed.status_code, 202)
        self.assertEqual(completed.json()["model"], "base")
        self.assertEqual(completed.json()["language"], "ru")

    async def test_cancel_upload_removes_partial_file_and_session(self):
        created = await self.client.post("/api/uploads", json={"filename": "partial.wav", "size": 8})
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"part")
        path = Path(self.app.state.db.get_upload(upload_id)["path"])

        cancelled = await self.client.delete(f"/api/uploads/{upload_id}")

        self.assertEqual(cancelled.status_code, 204)
        self.assertFalse(path.exists())
        self.assertEqual((await self.client.head(f"/api/uploads/{upload_id}")).status_code, 404)

    async def test_cancel_completed_upload_is_rejected_without_deleting_audio(self):
        created = await self.client.post("/api/uploads", json={"filename": "ready.wav", "size": 4})
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"data")
        job = (await self.client.post(f"/api/uploads/{upload_id}/complete")).json()

        cancelled = await self.client.delete(f"/api/uploads/{upload_id}")

        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(Path(job["audio_path"]).read_bytes(), b"data")
        self.assertEqual(self.app.state.db.get_job(job["id"])["status"], "queued")

    async def test_foreign_origin_cannot_mutate_local_app(self):
        response = await self.client.post("/api/uploads", headers={"Origin": "https://elsewhere.example"}, json={"filename": "x.wav", "size": 4})
        self.assertEqual(response.status_code, 403)

    async def test_resumed_upload_can_verify_existing_file_content(self):
        created = await self.client.post("/api/uploads", json={"filename": "same.wav", "size": 4, "model": "tiny"})
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        matching = await self.client.post(f"/api/uploads/{upload_id}/verify", json={"offset": 0, "length": 4, "sha256": digest})
        different = await self.client.post(f"/api/uploads/{upload_id}/verify", json={"offset": 0, "length": 4, "sha256": hashlib.sha256(b"diff").hexdigest()})
        self.assertEqual(matching.status_code, 200)
        self.assertTrue(matching.json()["matches"])
        self.assertFalse(different.json()["matches"])

    async def test_upload_listing_preserves_original_filename_for_resume_matching(self):
        created = await self.client.post(
            "/api/uploads",
            json={"filename": "meeting:2026.wav", "size": 8},
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"part")

        uploads = (await self.client.get("/api/uploads")).json()
        resumable = next(item for item in uploads if item["id"] == upload_id)

        self.assertEqual(resumable["filename"], "meeting_2026.wav")
        self.assertEqual(resumable["source_filename"], "meeting:2026.wav")

    async def test_homepage_shows_package_version(self):
        from audioscribe import __version__
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"v{__version__}", response.text)

    async def test_limit_and_invalid_model_are_rejected(self):
        over = await self.client.post("/api/uploads", json={"filename": "large.wav", "size": 33})
        self.assertEqual(over.status_code, 413)
        bad = await self.client.post("/api/uploads", json={"filename": "small.wav", "size": 4, "model": "unknown"})
        self.assertEqual(bad.status_code, 422)

    async def test_uploaded_audio_job_can_be_processed_and_exported(self):
        created = await self.client.post("/api/uploads", json={"filename": "meeting.wav", "size": 4, "model": "tiny"})
        upload_id = created.json()["id"]
        await self.client.patch(f"/api/uploads/{upload_id}", headers={"Upload-Offset": "0"}, content=b"RIFF")
        job = (await self.client.post(f"/api/uploads/{upload_id}/complete")).json()

        class FakeTranscriber:
            def transcribe(self, _pcm, _language):
                return [{"start": 0.2, "end": 0.8, "text": "Пример расшифровки"}]

        self.app.state.worker.process(
            job,
            FakeTranscriber(),
            duration_fn=lambda _path: 1.0,
            reader=lambda _path, _seconds: iter([(0, 0.0, np.zeros(16_000, dtype="<i2").tobytes())]),
        )
        transcript = await self.client.get(f"/api/jobs/{job['id']}/transcript?format=txt")
        subtitles = await self.client.get(f"/api/jobs/{job['id']}/transcript?format=srt")
        self.assertEqual(transcript.status_code, 200)
        self.assertIn("Пример расшифровки", transcript.text)
        self.assertIn("00:00:00,200 --> 00:00:00,800", subtitles.text)


if __name__ == "__main__":
    unittest.main()
