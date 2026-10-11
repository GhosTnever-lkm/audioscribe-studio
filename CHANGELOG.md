# Changelog

## 0.1.1 — 2026-10-12

- Add automated source archive and SHA-256 checksum to GitHub releases.
- Verify the release archive can be installed and imported in a clean Python environment.
- Exercise the real local faster-whisper tiny model on synthesized speech during this development cycle.

## 0.1.0 — 2026-10-12

- Initial local-first transcription app with a resumable, chunked upload flow.
- Five-gigabyte per-file limit, persistent queue, pause/resume checkpoints, and timestamped TXT/SRT/VTT/JSON exports.
- Local faster-whisper models and sequential PyAV decoding.
- Russian dark responsive web interface and Windows/Linux CI.
