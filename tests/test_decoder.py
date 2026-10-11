import tempfile
import unittest
import wave
from pathlib import Path

try:
    import av  # noqa: F401
except ImportError:
    av = None

import numpy as np

from audioscribe.worker import iter_pcm_chunks, probe_duration


@unittest.skipIf(av is None, "PyAV is installed by the optional transcription extra")
class DecoderTests(unittest.TestCase):
    def test_resamples_and_splits_real_wav_into_bounded_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "stereo-48khz.wav"
            t = np.arange(48_000, dtype=np.float64) / 48_000
            left = (np.sin(t * 2 * np.pi * 440) * 12000).astype("<i2")
            right = (np.sin(t * 2 * np.pi * 220) * 12000).astype("<i2")
            interleaved = np.column_stack((left, right)).reshape(-1).astype("<i2")
            with wave.open(str(source), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48_000)
                output.writeframes(interleaved.tobytes())

            self.assertAlmostEqual(probe_duration(source), 1.0, places=2)
            chunks = list(iter_pcm_chunks(source, chunk_seconds=0.4))
            self.assertEqual([index for index, _, _ in chunks], [0, 1, 2])
            self.assertEqual([offset for _, offset, _ in chunks], [0.0, 0.4, 0.8])
            self.assertEqual([len(pcm) for _, _, pcm in chunks], [12_800, 12_800, 6_400])


if __name__ == "__main__":
    unittest.main()
