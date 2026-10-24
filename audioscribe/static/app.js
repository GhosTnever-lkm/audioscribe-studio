const API = "";
const MAX_BYTES = 5_000_000_000;
const CHUNK_BYTES = 8 * 1024 * 1024;
const $ = (id) => document.getElementById(id);
const fileInput = $("audioFile");
const dropZone = $("dropZone");
let selectedFile = null;
let isUploading = false;
let toastTimer;

function humanSize(bytes) {
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let n = bytes, i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function notify(message, error = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.style.borderColor = error ? "#78413e" : "";
  toast.style.background = error ? "#281a1b" : "";
  toast.style.color = error ? "#ffc0ba" : "";
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 4200);
}

function selectFile(file) {
  if (!file) return;
  if (isUploading) { notify("Дождись завершения текущей загрузки, прежде чем выбирать другой файл.", true); return; }
  if (file.size > MAX_BYTES) { notify("Файл больше допустимого лимита 5 ГБ.", true); return; }
  if (file.size === 0) { notify("Файл пустой.", true); return; }
  selectedFile = file;
  $("fileName").textContent = file.name;
  $("fileSize").textContent = humanSize(file.size);
  $("filePreview").classList.remove("hidden");
  $("startButton").disabled = false;
}

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });
fileInput.addEventListener("change", () => selectFile(fileInput.files[0]));
$("removeFile").addEventListener("click", () => { if (isUploading) return; selectedFile = null; fileInput.value = ""; $("filePreview").classList.add("hidden"); $("startButton").disabled = true; });
for (const eventName of ["dragenter", "dragover"]) dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add("drag"); });
for (const eventName of ["dragleave", "drop"]) dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove("drag"); });
dropZone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try { const data = await response.json(); message = data.detail || message; } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function setUploadProgress(offset, total, state) {
  const pct = total ? Math.min(100, (offset / total) * 100) : 0;
  $("uploadBar").style.width = `${pct}%`;
  $("uploadPercent").textContent = `${Math.floor(pct)}%`;
  $("uploadTransferred").textContent = `${humanSize(offset)} / ${humanSize(total)}`;
  $("uploadStatus").textContent = state;
}

async function uploadedPrefixMatches(uploadId, file, offset) {
  if (!globalThis.crypto?.subtle) return false;
  for (let start = 0; start < offset; start += CHUNK_BYTES) {
    const bytes = await file.slice(start, Math.min(start + CHUNK_BYTES, offset)).arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    const result = await request(`/api/uploads/${uploadId}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ offset: start, length: bytes.byteLength, sha256 }),
    });
    if (!result.matches) return false;
  }
  return true;
}

async function uploadAndStart() {
  if (!selectedFile || isUploading) return;
  isUploading = true;
  const button = $("startButton");
  button.disabled = true;
  button.querySelector("span:first-child").textContent = "Загружаю файл…";
  $("uploadProgress").classList.remove("hidden");
  let uploadId;
  try {
    const open = await request("/api/uploads");
    const candidates = open
      .filter((item) => item.filename === selectedFile.name && item.size === selectedFile.size && item.offset > 0)
      .sort((a, b) => b.offset - a.offset);
    const canVerifyPrefix = !!globalThis.crypto?.subtle;
    let session = null;
    for (const candidate of candidates) {
      setUploadProgress(0, selectedFile.size, "Сверяю содержимое с незавершённой загрузкой…");
      if (await uploadedPrefixMatches(candidate.id, selectedFile, candidate.offset)) {
        session = candidate;
        break;
      }
    }
    if (session) {
      uploadId = session.id;
      notify("Содержимое совпало — продолжаю с сохранённого места.");
    } else {
      session = await request("/api/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: selectedFile.name, size: selectedFile.size, model: $("modelSelect").value, language: $("languageSelect").value || null }) });
      uploadId = session.id;
      if (candidates.length) {
        const message = canVerifyPrefix
          ? "Содержимое отличается от незавершённой загрузки. Начала отдельную сессию; прежняя сохранена."
          : "Не удалось проверить прежнюю загрузку. Начала отдельную сессию; прежняя сохранена.";
        notify(message);
      }
    }
    let offset = session.offset || 0;
    setUploadProgress(offset, selectedFile.size, "Загружаю порциями…");
    while (offset < selectedFile.size) {
      const end = Math.min(offset + CHUNK_BYTES, selectedFile.size);
      const chunk = selectedFile.slice(offset, end);
      try {
        const response = await fetch(`/api/uploads/${uploadId}`, { method: "PATCH", headers: { "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" }, body: chunk });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || "Не удалось передать блок файла.");
        }
        offset = Number(response.headers.get("Upload-Offset"));
      } catch (error) {
        // Ask the server what it committed, then safely retry from that exact byte offset.
        try {
          const head = await fetch(`/api/uploads/${uploadId}`, { method: "HEAD" });
          if (head.ok) {
            const actual = Number(head.headers.get("Upload-Offset"));
            if (actual !== offset) { offset = actual; continue; }
          }
        } catch {}
        throw error;
      }
      setUploadProgress(offset, selectedFile.size, "Загружаю порциями…");
    }
    setUploadProgress(offset, selectedFile.size, "Передаю в очередь…");
    await request(`/api/uploads/${uploadId}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("modelSelect").value, language: $("languageSelect").value || null }),
    });
    setUploadProgress(offset, selectedFile.size, "Задание добавлено");
    notify("Аудио добавлено в очередь расшифровки.");
    selectedFile = null;
    fileInput.value = "";
    $("filePreview").classList.add("hidden");
    button.querySelector("span:first-child").textContent = "Начать расшифровку";
    setTimeout(() => $("uploadProgress").classList.add("hidden"), 1700);
    await refreshJobs();
  } catch (error) {
    setUploadProgress(0, selectedFile?.size || 0, "Загрузка приостановлена");
    notify(`${error.message} Выбери этот же файл снова, чтобы продолжить загрузку.`, true);
  } finally {
    isUploading = false;
    button.querySelector("span:first-child").textContent = "Начать расшифровку";
    button.disabled = !selectedFile;
  }
}

$("startButton").addEventListener("click", uploadAndStart);

const statusLabels = { queued: "В очереди", running: "Расшифровывается", paused: "На паузе", failed: "Ошибка", completed: "Готово" };
function jobCard(job) {
  const article = document.createElement("article");
  article.className = "job-card";
  const left = document.createElement("div");
  const title = document.createElement("div"); title.className = "job-title"; title.textContent = job.filename;
  const sub = document.createElement("div"); sub.className = "job-sub";
  const duration = job.duration ? `${Math.floor(job.duration / 60)} мин` : "Длительность определяется";
  sub.textContent = `${job.model} · ${duration} · ${new Date(job.created_at + "Z").toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })}`;
  left.append(title, sub);
  if (["running", "queued", "paused"].includes(job.status)) { const track = document.createElement("div"); track.className = "job-progress"; const fill = document.createElement("div"); fill.style.width = `${Math.round((job.progress || 0) * 100)}%`; track.append(fill); left.append(track); }
  if (job.error) { const err = document.createElement("div"); err.className = "job-error"; err.textContent = job.error; left.append(err); }
  const right = document.createElement("div"); right.className = "job-actions";
  const pill = document.createElement("span"); pill.className = `status-pill ${job.status}`; pill.textContent = statusLabels[job.status] || job.status; right.append(pill);
  if (job.status === "completed") {
    const preview = document.createElement("a"); preview.className = "action-button"; preview.href = `/api/jobs/${job.id}/transcript?format=txt`; preview.textContent = "Открыть текст"; preview.target = "_blank"; preview.rel = "noreferrer"; right.append(preview);
    for (const fmt of ["txt", "srt", "vtt", "json"]) { const a = document.createElement("a"); a.className = "action-button"; a.href = `/api/jobs/${job.id}/transcript?format=${fmt}`; a.textContent = fmt.toUpperCase(); a.download = `transcript.${fmt}`; right.append(a); }
  }
  if (job.status === "running" || job.status === "queued") { const b = document.createElement("button"); b.className = "action-button"; b.textContent = "Пауза"; b.addEventListener("click", () => act(`/api/jobs/${job.id}/pause`, "POST")); right.append(b); }
  if (["paused", "failed"].includes(job.status)) { const b = document.createElement("button"); b.className = "action-button"; b.textContent = "Продолжить"; b.addEventListener("click", () => act(`/api/jobs/${job.id}/resume`, "POST")); right.append(b); }
  if (!["running", "queued"].includes(job.status)) { const b = document.createElement("button"); b.className = "action-button danger"; b.textContent = "Удалить"; b.addEventListener("click", () => act(`/api/jobs/${job.id}`, "DELETE")); right.append(b); }
  article.append(left, right);
  return article;
}

async function act(path, method) {
  try { await request(path, { method }); await refreshJobs(); } catch (error) { notify(error.message, true); }
}

async function refreshJobs() {
  try {
    const jobs = await request("/api/jobs");
    const list = $("jobsList");
    list.replaceChildren();
    $("jobsCount").textContent = `${jobs.length} ${jobs.length === 1 ? "запись" : "записей"}`;
    if (!jobs.length) {
      const empty = document.createElement("div"); empty.className = "empty-state";
      const icon = document.createElement("span"); icon.className = "empty-icon"; icon.textContent = "⌁";
      const copy = document.createElement("div"); const strong = document.createElement("strong"); strong.textContent = "Пока здесь пусто"; const p = document.createElement("p"); p.textContent = "Добавь аудиозапись — результат появится в этом списке."; copy.append(strong, p); empty.append(icon, copy); list.append(empty);
    } else jobs.forEach((job) => list.append(jobCard(job)));
  } catch {}
}

refreshJobs();
setInterval(refreshJobs, 3500);
