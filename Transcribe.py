"""
Stage 1, step 1: transcribe a lecture video into timestamped segments.
Run:  python transcribe.py path\to\lecture.mp4
Outputs: lecture.json (segments for search later) and lecture.vtt (subtitles)
"""
import json
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

MODEL_SIZE = "base"  # try "tiny" (faster) or "small" (more accurate) later


def fmt(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{s:06.3f}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python transcribe.py path\\to\\lecture.mp4")
        return
    video = Path(sys.argv[1])

    print(f"Loading model '{MODEL_SIZE}' (first run downloads it)...")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    start = time.time()
    segments, info = model.transcribe(str(video), vad_filter=True)
    print(f"Audio length: {info.duration / 60:.1f} min. Transcribing...")

    results = []
    for seg in segments:
        results.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                        "text": seg.text.strip()})
        print(f"[{fmt(seg.start)}] {seg.text.strip()}")

    elapsed = time.time() - start
    speed = info.duration / elapsed if elapsed else 0
    print(f"\nDone: {len(results)} segments in {elapsed / 60:.1f} min "
          f"({speed:.1f}x real time)")

    video.with_suffix(".json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    with open(video.with_suffix(".vtt"), "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for r in results:
            f.write(f"{fmt(r['start'])} --> {fmt(r['end'])}\n{r['text']}\n\n")
    print(f"Saved {video.with_suffix('.json').name} and {video.with_suffix('.vtt').name}")


if __name__ == "__main__":
    main()