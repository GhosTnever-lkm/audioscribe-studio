# Architecture

AudioScribe Studio is a single-user local web application. FastAPI serves the UI and API on loopback; the browser uses resumable byte-offset uploads; a background thread claims queued tasks from SQLite. Source audio and all result data remain under the configured local data directory.

## Upload lifecycle

1. `POST /api/uploads` validates declared size (5,000,000,000-byte hard limit), sanitizes the display filename and creates a random upload ID.
2. The browser sends fixed-size chunks with `PATCH` and `Upload-Offset`. The server checks the exact expected byte offset, caps each request, writes to a `.part` file and fsyncs before saving the acknowledged offset in SQLite. If a response is lost, the client reads `HEAD` and resumes at the server's committed offset.
3. `POST /complete` verifies the full length and atomically moves the completed file into the audio directory before it queues a job.

## Transcription lifecycle

The worker reads duration metadata through PyAV, then decodes the selected audio stream sequentially into mono 16 kHz signed-16-bit PCM windows. The working PCM plus a two-second overlap is bounded to one processing window. faster-whisper receives a normalized NumPy float32 array for one window. Segment timestamps are shifted to the global timeline; each segment is assigned to the processing window containing its midpoint, so overlap text is stored once. Transcript rows and the completed chunk marker are committed together in one SQLite transaction. On retry, completed leading windows are decoded again to reconstruct overlap context, but inference resumes at the first unfinished window. This trades some retry decode time for reliable resume behavior across container formats without relying on seeking semantics.

## Persistence

- `uploads`: upload identity, path, declared byte count and last acknowledged byte offset.
- `jobs`: source path, selected model/language, state, progress and errors.
- `chunks`: contiguous checkpoints with source time ranges.
- `segments`: absolute start/end times and text.

SQLite WAL mode supports readers while the worker writes. Checkpointing makes a stopped job resumable, but hard-killing the process during an inference call means that current block will be repeated.

## Trust boundary

The app binds to `127.0.0.1` by default and rejects mutation requests with a cross-origin `Origin`. Upload filenames are reduced to a basename; API paths are never interpolated into a shell command. An upload block is bounded before it is joined in memory. The worker accepts only the fixed set of model identifiers presented by the UI. There is no public multi-user authentication or encryption at rest; this release is for one user's local machine.

