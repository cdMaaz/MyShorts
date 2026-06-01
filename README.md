# MyShorts — AI Viral Clip Generator

Independent, clean-room build. All 4 known OpenShorts issues fixed:

| Bug | Fix |
|-----|-----|
| **Clips not loading** | Results written to `results.json` after **every single clip** — frontend sees them as they finish via polling |
| **Hooks not added** | Hook text from Gemini is burned directly with FFmpeg `drawtext` in the effects pass |
| **Auto edit / zoom not working** | Slow zoom-in via FFmpeg `zoompan` applied in the same effects pass |
| **AI subtitles not working** | `faster-whisper` word timestamps → TikTok-style karaoke ASS file → burned with FFmpeg `ass` filter |

---

## Quick start (Docker — recommended)

```bash
git clone <your-repo>
cd myshorts

docker compose up --build
```

Open **http://localhost:8000**

1. Click the ⚙️ Settings icon and paste your [Gemini API key](https://aistudio.google.com/app/apikey) (free).
2. Paste a YouTube URL or upload a video.
3. Toggle which effects you want: **Hooks**, **Zoom**, **Subtitles**.
4. Click **Generate Clips** and watch clips appear in real time.

---

## Local dev (no Docker)

### Backend

```bash
cd backend
pip install -r requirements.txt
# ffmpeg must be installed: sudo apt install ffmpeg  (or brew install ffmpeg on Mac)
uvicorn app:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173  (proxies API to :8000)
```

---

## Architecture

```
myshorts/
├── backend/
│   ├── app.py          FastAPI: job queue, SSE log stream, /clips static serve
│   ├── pipeline.py     Orchestrator: download → transcribe → Gemini → process clips
│   ├── processor.py    Per-clip: cut → 9:16 crop (face-track or blurred-bg) → effects
│   ├── subtitles.py    faster-whisper timestamps → TikTok-style ASS file
│   └── requirements.txt
└── frontend/
    └── src/
        ├── App.jsx             Job lifecycle, polling, clip grid
        └── components/
            ├── ProcessForm.jsx  URL / upload form + effect toggles
            ├── ClipCard.jsx     Video player + metadata + download
            ├── LogViewer.jsx    SSE log stream
            └── Settings.jsx     Gemini key (stored in localStorage)
```

### Pipeline flow

```
Input (URL or file)
  │
  ├─ yt-dlp download          [pipeline.py]
  ├─ faster-whisper transcribe [pipeline.py]
  ├─ Gemini 2.5 Flash analyze  [pipeline.py]
  │     → 3-15 viral moments with timestamps + hook text
  │
  └─ For each clip:
        ├─ FFmpeg cut (re-encode, frame-accurate)   [processor.py]
        ├─ 9:16 crop:
        │     TRACK   — MediaPipe face detect + smooth pan → pipe to FFmpeg
        │     GENERAL — FFmpeg blurred-background filter (no OpenCV loop)
        ├─ Generate ASS subtitles from word timestamps [subtitles.py]
        └─ Effects pass (single FFmpeg call):
              • zoompan  — slow 1.0→1.08 zoom
              • drawtext — hook text overlay at top
              • ass      — burned karaoke subtitles
        → write to results.json immediately ← frontend polls this
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/jobs` | Start job. Body: form-data with `url`/`file` + `hooks`, `zoom`, `subtitles` booleans. Header: `X-Gemini-Key` |
| `GET` | `/api/jobs/{id}` | Poll status + incremental clips |
| `GET` | `/api/jobs/{id}/logs?after=N` | SSE log stream |
| `DELETE` | `/api/jobs/{id}` | Cancel job |
| `GET` | `/clips/{id}/{filename}` | Serve processed clip |
| `GET` | `/api/health` | Health check |

---

## Requirements

- Docker & Docker Compose **or** Python 3.10+, Node 18+, FFmpeg
- [Google Gemini API key](https://aistudio.google.com/app/apikey) — free tier works

---

## License

MIT
