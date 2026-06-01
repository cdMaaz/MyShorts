"""
pipeline.py
Main pipeline orchestrator. Run as a standalone subprocess by app.py.

Usage:
  python pipeline.py \
    --job-id <id> \
    --output-dir <dir> \
    [--url <youtube_url> | --input <file_path>] \
    [--hooks] [--zoom] [--subtitles] \
    [--font <path>]

Progress is printed to stdout (captured by app.py and streamed to the UI).
Results are written incrementally to <output-dir>/results.json after every clip.
"""

import argparse
import json
import os
import sys
import time
import warnings
import yt_dlp
import cv2

warnings.filterwarnings("ignore")

from processor import process_clip

# ── Gemini prompt ──────────────────────────────────────────────────────────────

GEMINI_PROMPT = """\
You are a world-class short-form video strategist who has grown multiple accounts to millions of followers.
Analyze this transcript and extract the {max_clips} HIGHEST-PERFORMING moments for TikTok/Reels/YouTube Shorts.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTENT CATEGORIES (pick the best category per clip):

🔥 FUNNY / COMEDY
  Unexpected punchlines, reactions, fails, self-deprecating humor, absurd comparisons.
  Hook style: "Wait for it..." / "I can't believe he said this 💀" / "This gets better..."

⚡ MOTIVATIONAL / MINDSET
  Shift in perspective, hard truth, success principle, overcoming struggle.
  Hook style: "Nobody talks about this" / "This changed everything" / "Listen carefully..."

🎭 CONTROVERSIAL / DEBATE
  Unpopular opinion, counter-narrative, challenging conventional wisdom.
  Hook style: "Unpopular opinion:" / "Everyone is wrong about this" / "Hot take:"

😭 EMOTIONAL / STORY
  Vulnerable moment, personal story, relatable struggle, heartfelt realization.
  Hook style: "I never told anyone this" / "This is why I..." / "True story:"

🧠 EDUCATIONAL / SURPRISING
  Counterintuitive fact, "did you know", expert insight, reveals something hidden.
  Hook style: "Most people don't know..." / "The truth about..." / "Here's the secret:"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SELECTION RULES:
1. Each clip MUST be 15–55 seconds (hard limit — do NOT exceed).
2. Timestamps are ABSOLUTE SECONDS from video start.
3. Start clip 1–3 seconds BEFORE the key moment (let it breathe).
4. End clip 2–3 seconds AFTER the key moment (let the point land).
5. Prioritize: complete thoughts > partial sentences (never cut mid-sentence).
6. viral_hook_text: max 8 words, ALL CAPS, in SAME LANGUAGE as transcript.
   Must create FOMO or curiosity. Never generic ("Watch this", "Check this out").
7. Variety: aim for at least 2 different categories across all clips.
8. Order by predicted virality (best performing first).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VIDEO DURATION: {duration}s

TRANSCRIPT:
{transcript}

WORD TIMESTAMPS (w=word, s=start_sec, e=end_sec):
{words_json}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT: Return ONLY valid JSON. No markdown fences. No preamble.

{{
  "shorts": [
    {{
      "start": <float seconds>,
      "end": <float seconds>,
      "category": "<FUNNY|MOTIVATIONAL|CONTROVERSIAL|EMOTIONAL|EDUCATIONAL>",
      "title": "<YouTube Shorts title, max 80 chars, no clickbait>",
      "description_tiktok": "<150 chars + 5 relevant hashtags>",
      "description_instagram": "<200 chars + 8 relevant hashtags>",
      "viral_hook_text": "<ALL CAPS, max 8 words, creates curiosity/FOMO>"
    }}
  ]
}}
"""


# ── Downloader ─────────────────────────────────────────────────────────────────

def download_video(url: str, output_dir: str) -> str:
    """Download YouTube video, return local file path."""
    print("📥 Downloading video...", flush=True)

    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "format": "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][ext=mp4]+bestaudio/bestvideo+bestaudio/best",
        "outtmpl": os.path.join(output_dir, "source.%(ext)s"),
        "merge_output_format": "mp4",
        "overwrites": True,
        "retries": 5,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embed", "android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    }

    # Write cookies if provided via env
    cookies_env = os.environ.get("YOUTUBE_COOKIES")
    if cookies_env:
        cookies_path = os.path.join(output_dir, "cookies.txt")
        with open(cookies_path, "w") as f:
            f.write(cookies_env)
        ydl_opts["cookiefile"] = cookies_path
        print("🍪 Using provided cookies", flush=True)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Find downloaded file
    for ext in ["mp4", "mkv", "webm"]:
        p = os.path.join(output_dir, f"source.{ext}")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            print(f"✅ Downloaded: {p}", flush=True)
            return p

    raise FileNotFoundError("Download completed but output file not found.")


# ── Transcriber ────────────────────────────────────────────────────────────────

def transcribe(video_path: str) -> dict:
    """Transcribe video with faster-whisper. Returns {text, segments, language}."""
    print("🎙️  Transcribing (faster-whisper)...", flush=True)
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(video_path, word_timestamps=True)

    print(f"  Language: {info.language} (p={info.language_probability:.2f})", flush=True)

    segments = []
    full_text = ""
    for seg in segments_iter:
        words = []
        if seg.words:
            for w in seg.words:
                words.append({"word": w.word, "start": w.start, "end": w.end})
        segments.append({"text": seg.text, "start": seg.start, "end": seg.end, "words": words})
        full_text += seg.text + " "
        print(f"  [{seg.start:.1f}s] {seg.text.strip()}", flush=True)

    print(f"✅ Transcription done. Total chars: {len(full_text)}", flush=True)
    return {"text": full_text.strip(), "segments": segments, "language": info.language}


# ── Gemini analysis ────────────────────────────────────────────────────────────

def analyze_clips(transcript: dict, duration: float, max_clips: int = 10) -> list:
    """Ask Gemini to identify viral moments. Returns list of clip dicts."""
    print("🤖 Analyzing with Gemini...", flush=True)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    from google import genai as google_genai

    client = google_genai.Client(api_key=api_key)

    # Build compact word list
    words = []
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            words.append({"w": w["word"], "s": round(w["start"], 2), "e": round(w["end"], 2)})

    prompt = GEMINI_PROMPT.format(
        max_clips=max_clips,
        duration=round(duration, 1),
        transcript=transcript["text"][:8000],
        words_json=json.dumps(words[:2000]),
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    raw = response.text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
    raw = raw.strip()

    data = json.loads(raw)
    shorts = data.get("shorts", [])
    print(f"✅ Gemini found {len(shorts)} clip candidates", flush=True)

    # Validate and fix timestamps
    valid = []
    for s in shorts:
        start = float(s.get("start", 0))
        end = float(s.get("end", 0))
        if end <= start:
            continue
        if end - start < 10:  # sanity: at least 10s
            continue
        if start < 0:
            start = 0
        if end > duration:
            end = duration
        s["start"] = start
        s["end"] = end
        valid.append(s)

    print(f"  {len(valid)} clips after validation", flush=True)
    return valid


# ── Results helpers ────────────────────────────────────────────────────────────

def _write_results(results_path: str, data: dict):
    """Atomically write results JSON (write temp + rename)."""
    tmp = results_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, results_path)


# ── Main ───────────────────────────────────────────────────────────────────────

def run(args):
    job_id = args.job_id
    job_dir = args.output_dir
    os.makedirs(job_dir, exist_ok=True)

    results_path = os.path.join(job_dir, "results.json")

    # ── Initial state ──────────────────────────────────────────────────────────
    results = {
        "status": "running",
        "stage": "starting",
        "clips": [],
        "error": None,
        "transcript": None,
    }
    _write_results(results_path, results)

    try:
        # ── Get source video ───────────────────────────────────────────────────
        if args.url:
            results["stage"] = "downloading"
            _write_results(results_path, results)
            source_video = download_video(args.url, job_dir)
        else:
            source_video = args.input
            if not os.path.exists(source_video):
                raise FileNotFoundError(f"Input not found: {source_video}")
            print(f"📂 Using uploaded file: {source_video}", flush=True)

        # ── Get duration ───────────────────────────────────────────────────────
        cap = cv2.VideoCapture(source_video)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps
        cap.release()
        print(f"📏 Duration: {duration:.1f}s", flush=True)

        # ── Transcribe ─────────────────────────────────────────────────────────
        results["stage"] = "transcribing"
        _write_results(results_path, results)
        transcript = transcribe(source_video)
        results["transcript"] = transcript

        # ── Analyze ────────────────────────────────────────────────────────────
        results["stage"] = "analyzing"
        _write_results(results_path, results)
        clip_candidates = analyze_clips(transcript, duration)

        if not clip_candidates:
            raise RuntimeError("Gemini returned no valid clip candidates.")

        # ── Process clips ──────────────────────────────────────────────────────
        results["stage"] = "processing"
        results["total_clips"] = len(clip_candidates)
        _write_results(results_path, results)

        for i, clip_data in enumerate(clip_candidates):
            print(f"\n🎬 Processing clip {i+1}/{len(clip_candidates)}", flush=True)

            # SIGNAL: app.py parses this to update live status
            print(f"PROGRESS:clip_start:{i+1}/{len(clip_candidates)}", flush=True)

            final_path = process_clip(
                source_video=source_video,
                clip_start=clip_data["start"],
                clip_end=clip_data["end"],
                job_dir=job_dir,
                clip_index=i,
                hook_text=clip_data.get("viral_hook_text"),
                apply_hooks=args.hooks,
                apply_zoom=args.zoom,
                apply_subtitles=args.subtitles,
                transcript_segments=transcript["segments"],
                font_path=args.font,
            )

            if final_path and os.path.exists(final_path):
                filename = os.path.basename(final_path)
                results["clips"].append({
                    "index": i,
                    "filename": filename,
                    # URL path — app.py serves /clips/<job_id>/<filename>
                    "url": f"/clips/{job_id}/{filename}",
                    "title": clip_data.get("title", f"Clip {i+1}"),
                    "description_tiktok": clip_data.get("description_tiktok", ""),
                    "description_instagram": clip_data.get("description_instagram", ""),
                    "hook_text": clip_data.get("viral_hook_text", ""),
                    "start": clip_data["start"],
                    "end": clip_data["end"],
                    "duration": round(clip_data["end"] - clip_data["start"], 1),
                })
                # Write after EVERY clip — this is the key fix for "clips not loading"
                _write_results(results_path, results)
                print(f"PROGRESS:clip_done:{i+1}/{len(clip_candidates)}:{filename}", flush=True)
            else:
                print(f"⚠️  Clip {i+1} failed, skipping", flush=True)

        # ── Done ───────────────────────────────────────────────────────────────
        results["status"] = "completed"
        results["stage"] = "done"
        _write_results(results_path, results)
        print(f"\n✅ Pipeline complete. {len(results['clips'])} clips ready.", flush=True)
        print(f"PIPELINE_COMPLETE:{len(results['clips'])}", flush=True)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"\n❌ Pipeline error: {e}", flush=True)
        print(tb, flush=True)
        results["status"] = "failed"
        results["error"] = str(e)
        _write_results(results_path, results)
        print(f"PIPELINE_FAILED:{e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", type=str)
    group.add_argument("--input", type=str)

    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--hooks", action="store_true", default=True)
    parser.add_argument("--no-hooks", dest="hooks", action="store_false")
    parser.add_argument("--zoom", action="store_true", default=True)
    parser.add_argument("--no-zoom", dest="zoom", action="store_false")
    parser.add_argument("--subtitles", action="store_true", default=True)
    parser.add_argument("--no-subtitles", dest="subtitles", action="store_false")
    parser.add_argument("--font", type=str, default=None)

    args = parser.parse_args()
    run(args)
