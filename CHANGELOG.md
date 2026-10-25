# Changelog

## 0.1.9 — 2026-10-25

- Preserve the original upload filename so files whose names need sanitization can resume after an interrupted upload.
- Keep the sanitized filename for stored output and migrate existing upload rows without changing their stored filenames.

## 0.1.8 — 2026-10-24

- Apply the currently selected model and language when creating a transcription job, including after resuming an interrupted upload.

## 0.1.7 — 2026-10-24

- Serialize upload cancellation with active chunk writes and job completion, preventing failed cancels and orphaned audio files.
- Update GitHub Actions to checkout v7 and setup-python v7, and grant CI read-only repository access.
- Pin the release runner to Ubuntu 24.04 for reproducible packaging.

## 0.1.6 — 2026-10-12

- Verify the content of each acknowledged upload block before resuming, so a different file with the same name and size starts a separate upload instead of corrupting the audio.
- Keep the selected file fixed while an upload is running.
- Use current SPDX license metadata in the Python package build.

## 0.1.5 — 2026-10-12

- Serialize upload chunk commits within the local app process so simultaneous retries at one offset cannot append the same bytes twice.
- Add a regression check for competing requests using the same acknowledged offset.

## 0.1.4 — 2026-10-12

- Render the sidebar version from package metadata and cover it with an API test.

## 0.1.3 — 2026-10-12

- Display the current release version in the application sidebar.

## 0.1.2 — 2026-10-12

- Align the README and package metadata with the latest public release and link directly to release downloads.

## 0.1.1 — 2026-10-12

- Add automated source archive and SHA-256 checksum to GitHub releases.
- Verify the release archive can be installed and imported in a clean Python environment.
- Exercise the real local faster-whisper tiny model on synthesized speech during this development cycle.

## 0.1.0 — 2026-10-12

- Initial local-first transcription app with a resumable, chunked upload flow.
- Five-gigabyte per-file limit, persistent queue, pause/resume checkpoints, and timestamped TXT/SRT/VTT/JSON exports.
- Local faster-whisper models and sequential PyAV decoding.
- Russian dark responsive web interface and Windows/Linux CI.
