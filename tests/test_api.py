import tempfile
import unittest
from pathlib import Path

import httpx
import numpy as np

from audioscribe.config import Settings
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

    async def test_foreign_origin_cannot_mutate_local_app(self):
        response = await self.client.post("/api/uploads", headers={"Origin": "https://elsewhere.example"}, json={"filename": "x.wav", "size": 4})
        self.assertEqual(response.status_code, 403)

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
