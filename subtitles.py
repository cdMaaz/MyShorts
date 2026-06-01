"""
subtitles.py
Converts faster-whisper word timestamps into FFmpeg drawtext filter strings.
No ASS files, no path escaping issues, works on Windows/Linux/Mac.

TikTok-style: UPPERCASE, 3 words at a time, bold white with black outline.
"""

from typing import List, Dict, Optional

WORDS_PER_GROUP = 3


def _escape_dt(text: str) -> str:
    """Escape text for FFmpeg drawtext filter."""
    return (text
            .replace("\\", "\\\\")
            .replace("'",  "\u2019")   # curly apostrophe avoids shell quoting issues
            .replace(":",  "\\:")
            .replace(",",  "\\,")
            .replace("[",  "\\[")
            .replace("]",  "\\]")
            .replace(";",  "\\;")
            .replace("%",  "\\%"))


def get_word_groups(segments: List[Dict], clip_start: float, clip_end: float) -> List[Dict]:
    """
    Extract words for a clip and group them into subtitle units.
    Returns list of {text, start, end} with timestamps relative to clip start.
    """
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            ws, we = float(w["start"]), float(w["end"])
            if we < clip_start or ws > clip_end:
                continue
            word = w["word"].strip()
            if not word:
                continue
            words.append({
                "word": word,
                "start": max(0.0, ws - clip_start),
                "end":   min(clip_end - clip_start, we - clip_start),
            })

    groups = []
    for i in range(0, len(words), WORDS_PER_GROUP):
        chunk = words[i:i + WORDS_PER_GROUP]
        if not chunk:
            continue
        gs = chunk[0]["start"]
        ge = chunk[-1]["end"]
        if ge <= gs:
            ge = gs + 0.5
        groups.append({
            "text":  " ".join(w["word"] for w in chunk).upper(),
            "start": gs,
            "end":   ge,
        })
    return groups


def make_subtitle_filters(
    word_groups: List[Dict],
    font_path:   Optional[str] = None,
    y_pos:       str = "h*0.77",
    fontsize:    int = 68,
) -> List[str]:
    """
    Convert word groups into FFmpeg drawtext filter strings.
    Each group is shown for its time window using enable='between(t,...)'
    """
    if not word_groups:
        return []

    # Build font argument (Windows-safe path)
    if font_path:
        safe_font = font_path.replace("\\", "/")
        # Escape colon in drive letter: C:/... → C\:/...
        if len(safe_font) > 1 and safe_font[1] == "/":
            safe_font = safe_font[0] + "\\:" + safe_font[2:]
        font_arg = f":fontfile='{safe_font}'"
    else:
        font_arg = ""

    filters = []
    for g in word_groups:
        safe_text = _escape_dt(g["text"])
        f = (
            f"drawtext="
            f"text='{safe_text}'"
            f"{font_arg}"
            f":fontsize={fontsize}"
            f":fontcolor=white"
            f":x=(w-text_w)/2"
            f":y={y_pos}"
            f":box=1"
            f":boxcolor=black@0.75"
            f":boxborderw=12"
            f":shadowx=3"
            f":shadowy=3"
            f":shadowcolor=black@0.9"
            f":enable='between(t,{g['start']:.3f},{g['end']:.3f})'"
        )
        filters.append(f)
    return filters


# Kept for backward compat — not used anymore
def generate_ass(*args, **kwargs):
    pass

def extract_clip_words(segments, clip_start, clip_end):
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            ws, we = float(w["start"]), float(w["end"])
            if we >= clip_start and ws <= clip_end:
                words.append({"word": w["word"], "start": ws, "end": we})
    return words
