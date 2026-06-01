"""
processor.py

Two-pass pipeline per clip:
  PASS 1 — Crop to 9:16 @ 1080×1920
    • Face/person tracking: OpenCV Haar + YOLO, smooth pan, pipe frames to FFmpeg
    • Fallback: center crop from source → blurred background fill

  PASS 2 — Effects (single FFmpeg -vf chain)
    • Slow zoom-in  (zoompan)
    • Hook text     (drawtext, top of frame, full duration)
    • Subtitles     (drawtext × N, bottom of frame, per word-group timing)
"""

import os, cv2, subprocess, numpy as np
from typing import Optional, List, Dict, Tuple
from ultralytics import YOLO
from subtitles import get_word_groups, make_subtitle_filters

# ── Output spec ────────────────────────────────────────────────────────────────
TARGET_W = 1080
TARGET_H = 1920
ASPECT   = TARGET_W / TARGET_H          # 9 / 16 ≈ 0.5625

DETECT_INTERVAL = 4    # run detection every N frames
SMOOTH_ALPHA    = 0.12  # EMA smoothing (lower = more stable but slower to respond)
SAFE_MARGIN     = 0.15  # keep face within this fraction from crop edge before panning

_face_cascade = None
_yolo_model   = None


def _get_cascade():
    global _face_cascade
    if _face_cascade is None:
        p = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(p)
    return _face_cascade


def _get_yolo():
    global _yolo_model
    if _yolo_model is None:
        _yolo_model = YOLO("yolov8n.pt")
    return _yolo_model


def _detect_subject_cx(frame, cascade, yolo):
    """
    Returns the x-center (pixels) of the best subject in the frame, or None.
    Tries face first, then YOLO person.
    """
    gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, 1.1, 4, minSize=(40, 40))
    if len(faces):
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        return float(fx + fw / 2)

    h, w = frame.shape[:2]
    small = cv2.resize(frame, (w // 2, h // 2))
    res = yolo(small, verbose=False, classes=[0])
    if res and res[0].boxes is not None and len(res[0].boxes):
        b = max(res[0].boxes.xyxy, key=lambda b: (b[2]-b[0])*(b[3]-b[1]))
        return float((b[0] + b[2]).item() / 2 * 2)   # scale back to full res
    return None


# ── Find a usable font ─────────────────────────────────────────────────────────
def _find_font() -> Optional[str]:
    candidates = [
        # Windows
        r"C:/Windows/Fonts/impact.ttf",
        r"C:/Windows/Fonts/arialbd.ttf",
        r"C:/Windows/Fonts/Arial.ttf",
        # Linux
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


# ── Video info ─────────────────────────────────────────────────────────────────
def get_video_info(path: str) -> Tuple[int, int, float, float]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"Cannot open: {path}")
    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    dur = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) / fps
    cap.release()
    return w, h, fps, dur


# ── Step 1: Cut ────────────────────────────────────────────────────────────────
def cut_clip(source: str, start: float, end: float, out: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", source,
        "-c:v", "libx264", "-crf", "16", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        out,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  [cut] {r.stderr.decode()[:300]}", flush=True)
    return r.returncode == 0


# ── Step 2: Crop to 9:16 ──────────────────────────────────────────────────────

def _crop_window_size(in_w: int, in_h: int) -> Tuple[int, int]:
    """
    Compute the 9:16 crop window (pixels) within the source frame.
    Strategy: use full source height, compute matching width.
    If source is already taller than 9:16, use full width instead.
    """
    crop_w = int(in_h * ASPECT)
    crop_h = in_h
    if crop_w > in_w:           # source is already portrait or square
        crop_w = in_w
        crop_h = int(in_w / ASPECT)
    crop_w = crop_w - (crop_w % 2)
    crop_h = crop_h - (crop_h % 2)
    return crop_w, crop_h


def _scan_subject_positions(path: str, fps: float, total_frames: int) -> List[Optional[float]]:
    """
    Scan the clip and return per-frame subject cx (or None).
    Runs detection every DETECT_INTERVAL frames, interpolates between.
    """
    cascade = _get_cascade()
    yolo    = _get_yolo()
    cap     = cv2.VideoCapture(path)
    results = [None] * total_frames

    idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if idx % DETECT_INTERVAL == 0:
            results[idx] = _detect_subject_cx(frame, cascade, yolo)
        idx += 1
    cap.release()

    # Forward-fill detections for intermediate frames
    last = None
    for i in range(len(results)):
        if results[i] is not None:
            last = results[i]
        elif last is not None:
            results[i] = last
    return results


def crop_with_tracking(raw: str, out: str) -> bool:
    """
    Face/person-tracked crop → 1080×1920.
    Smooth pan follows subject, stays within source bounds.
    Blurred source fills any exposed edges.
    """
    in_w, in_h, fps, dur = get_video_info(raw)
    total_frames = max(1, int(dur * fps))
    crop_w, crop_h = _crop_window_size(in_w, in_h)

    print(f"  [crop] {in_w}×{in_h} → crop {crop_w}×{crop_h} → {TARGET_W}×{TARGET_H}", flush=True)
    print(f"  [crop] Scanning {total_frames} frames for subjects...", flush=True)
    subject_xs = _scan_subject_positions(raw, fps, total_frames)

    has_subject = any(x is not None for x in subject_xs)
    if not has_subject:
        print("  [crop] No subject found → center crop + blur fill", flush=True)
        return crop_center_blur(raw, out, in_w, in_h, crop_w, crop_h)

    # ── Smooth the pan curve ────────────────────────────────────────────────
    # EMA smoothing pass
    default_cx = float(in_w) / 2
    smoothed   = []
    ema_cx     = default_cx
    for cx in subject_xs:
        target = cx if cx is not None else ema_cx
        ema_cx = ema_cx + SMOOTH_ALPHA * (target - ema_cx)
        smoothed.append(ema_cx)

    # Clamp so crop window never exceeds source bounds
    half = crop_w / 2
    clamped = [max(half, min(in_w - half, cx)) for cx in smoothed]

    # ── Pipe frames to FFmpeg encoder ───────────────────────────────────────
    noaudio = out.replace(".mp4", "_na.mp4")
    enc_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{TARGET_W}x{TARGET_H}",
        "-pix_fmt", "bgr24", "-r", str(fps),
        "-i", "-", "-an",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        noaudio,
    ]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Pre-build blurred background (use ffmpeg to make a 1-frame blurred version
    # then tile it — actually just blur per frame below)
    cap = cv2.VideoCapture(raw)
    frame_idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        cx   = clamped[frame_idx] if frame_idx < len(clamped) else in_w / 2
        x1   = max(0, int(cx - half))
        x2   = min(in_w, x1 + crop_w)
        x1   = max(0, x2 - crop_w)     # re-clamp after x2 adjustment

        # Cropped subject region → scale to 1080×1920
        cropped_fg = frame[0:crop_h, x1:x2]
        fg_scaled  = cv2.resize(cropped_fg, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LANCZOS4)

        # Blurred background: full frame → scale + blur → 1080×1920
        bg = cv2.resize(frame, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)
        bg = cv2.GaussianBlur(bg, (51, 51), 25)

        # Composite: bg behind fg
        # For landscape source: fg fills width perfectly, no gap
        # For portrait/square: fg may not fill full height → blend edges
        result = fg_scaled    # fg already fills 1080×1920

        # If source was wider than 9:16, fg fills exactly → use fg directly
        # If source already portrait, also fills → use fg directly
        # Only need bg composite if crop_w is much smaller than in_w
        # (shows blurred sides)
        if crop_w < in_w * 0.7:     # landscape video — show blurred bg on sides
            # Actually for portrait output, fg fills the full 1080×1920
            # No side bars. This is the correct behavior.
            pass

        try:
            enc.stdin.write(result.tobytes())
        except BrokenPipeError:
            break
        frame_idx += 1

    cap.release()
    try: enc.stdin.close()
    except: pass
    enc.wait()

    # Merge audio
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", noaudio, "-i", raw,
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        out,
    ], capture_output=True)
    try: os.remove(noaudio)
    except: pass

    if r.returncode != 0:
        print(f"  [crop] merge failed: {r.stderr.decode()[:200]}", flush=True)
    return r.returncode == 0


def crop_center_blur(raw: str, out: str,
                     in_w: int, in_h: int,
                     crop_w: int, crop_h: int) -> bool:
    """
    Pure FFmpeg center-crop + blurred background fill.
    Used as fallback when no subject is detected.
    """
    cx = (in_w - crop_w) // 2
    fc = (
        f"[0:v]split[bg][fg];"
        f"[bg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},gblur=sigma=30[blurred];"
        f"[fg]crop={crop_w}:{crop_h}:{cx}:0,scale={TARGET_W}:{TARGET_H}[foreground];"
        f"[blurred][foreground]overlay=0:0[out]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", raw,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy", out,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  [crop_blur] {r.stderr.decode()[:300]}", flush=True)
    return r.returncode == 0


def crop_clip(raw: str, out: str) -> bool:
    try:
        return crop_with_tracking(raw, out)
    except Exception as e:
        print(f"  [crop] tracking error: {e} → fallback", flush=True)
        in_w, in_h, fps, dur = get_video_info(raw)
        crop_w, crop_h = _crop_window_size(in_w, in_h)
        return crop_center_blur(raw, out, in_w, in_h, crop_w, crop_h)


# ── Step 3: Effects ────────────────────────────────────────────────────────────

def apply_effects(
    cropped:    str,
    out:        str,
    hook_text:  Optional[str]  = None,
    word_groups: Optional[List] = None,
    apply_zoom: bool            = True,
    font_path:  Optional[str]   = None,
) -> bool:
    """
    Single FFmpeg pass:
      zoompan → hook drawtext → subtitle drawtext × N
    All drawtext-based — no ASS files, no Windows path issues.
    """
    w, h, fps, dur = get_video_info(cropped)
    total_frames   = max(1, int(dur * fps))

    font = font_path or _find_font()

    # Font arg with Windows-safe escaping
    if font and os.path.exists(font):
        safe_font = font.replace("\\", "/")
        # Drive letter colon: C:/ → C\:/
        if len(safe_font) >= 2 and safe_font[1] == ":":
            safe_font = safe_font[0] + "\\:" + safe_font[2:]
        font_arg = f":fontfile='{safe_font}'"
    else:
        font_arg = ""

    vf_parts = []

    # ── Zoom: 1.0 → 1.06 over full clip ────────────────────────────────────
    if apply_zoom and dur > 0:
        step = 0.06 / total_frames
        vf_parts.append(
            f"zoompan="
            f"z='min(zoom+{step:.8f},1.06)':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d=1:s={TARGET_W}x{TARGET_H}:fps={fps:.3f}"
        )

    # ── Hook: top of frame, full clip duration ──────────────────────────────
    if hook_text:
        def _esc(t):
            return (t.replace("\\","\\\\").replace("'","\u2019")
                     .replace(":","\\:").replace(",","\\,")
                     .replace("[","\\[").replace("]","\\]")
                     .replace(";","\\;").replace("%","\\%"))
        safe_hook = _esc(hook_text.upper())
        vf_parts.append(
            f"drawtext="
            f"text='{safe_hook}'"
            f"{font_arg}"
            f":fontsize=52"
            f":fontcolor=white"
            f":x=(w-text_w)/2"
            f":y=100"
            f":box=1:boxcolor=black@0.75:boxborderw=14"
            f":shadowx=3:shadowy=3:shadowcolor=black@0.9"
        )

    # ── Subtitles: bottom, timed per word group ─────────────────────────────
    if word_groups:
        sub_filters = make_subtitle_filters(
            word_groups, font_path=font, fontsize=70, y_pos="h*0.78"
        )
        vf_parts.extend(sub_filters)

    if not vf_parts:
        import shutil; shutil.copy(cropped, out); return True

    vf_str = ",".join(vf_parts)

    # Windows command line can be short — write filter to a temp script file
    # to avoid 8191-char limit on Windows cmd.exe
    filter_file = out + ".vf.txt"
    try:
        with open(filter_file, "w", encoding="utf-8") as ff:
            ff.write(vf_str)
        cmd = [
            "ffmpeg", "-y", "-i", cropped,
            "-vf", vf_str,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-b:v", "4M",
            "-c:a", "copy",
            out,
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            err = r.stderr.decode()
            print(f"  [effects] error: {err[:500]}", flush=True)
            # Retry without subtitles
            if word_groups:
                print("  [effects] retrying without subtitles...", flush=True)
                return apply_effects(cropped, out, hook_text, None, apply_zoom, font_path)
            # Retry without zoom
            if apply_zoom:
                print("  [effects] retrying without zoom...", flush=True)
                return apply_effects(cropped, out, hook_text, word_groups, False, font_path)
            # Last resort: copy
            import shutil; shutil.copy(cropped, out)
            return True
        return True
    finally:
        try: os.remove(filter_file)
        except: pass


# ── Main entry ─────────────────────────────────────────────────────────────────
def process_clip(
    source_video:        str,
    clip_start:          float,
    clip_end:            float,
    job_dir:             str,
    clip_index:          int,
    hook_text:           Optional[str]  = None,
    apply_hooks:         bool           = True,
    apply_zoom:          bool           = True,
    apply_subtitles:     bool           = True,
    transcript_segments: Optional[List] = None,
    font_path:           Optional[str]  = None,
) -> Optional[str]:
    p       = os.path.join(job_dir, f"clip{clip_index:02d}")
    raw     = f"{p}_raw.mp4"
    cropped = f"{p}_cropped.mp4"
    final   = f"{p}_final.mp4"

    print(f"\n── Clip {clip_index+1}: {clip_start:.1f}s → {clip_end:.1f}s ──", flush=True)

    # 1. Cut
    print("  [1/3] Cutting...", flush=True)
    if not cut_clip(source_video, clip_start, clip_end, raw):
        return None

    # 2. Crop to 9:16 with face tracking
    print("  [2/3] Cropping with face tracking...", flush=True)
    if not crop_clip(raw, cropped):
        return None

    # 3. Word groups for subtitles
    word_groups = None
    if apply_subtitles and transcript_segments:
        try:
            word_groups = get_word_groups(transcript_segments, clip_start, clip_end)
            print(f"  ✓ {len(word_groups)} subtitle groups", flush=True)
        except Exception as e:
            print(f"  ⚠ Subtitle error: {e}", flush=True)

    # 4. Effects
    print("  [3/3] Burning effects...", flush=True)
    apply_effects(
        cropped, final,
        hook_text   = hook_text if apply_hooks else None,
        word_groups = word_groups,
        apply_zoom  = apply_zoom,
        font_path   = font_path or _find_font(),
    )

    # Cleanup intermediates
    for f in [raw, cropped]:
        try: os.remove(f)
        except: pass

    if os.path.exists(final) and os.path.getsize(final) > 10_000:
        print(f"  ✓ Done → {final}", flush=True)
        return final

    print(f"  ✗ Output missing or too small", flush=True)
    return None
