"""Transcribe a video with Whisper word timings.

Writes <out>.json ({"words": [...], "segments": [...]}) and prints a readable
transcript with pauses and retake cues marked, so the cut list can be picked.
"""
import argparse
import json
import re
import sys

CUES = re.compile(
    r"\b(cut (that|this|it) out|cut that|start (over|again)|let me (try|do) (that|it) again|"
    r"take (two|2|three|3)|one more time|scratch that)\b",
    re.I,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out", required=True, help="path for words JSON")
    p.add_argument("--model", default="small", help="tiny|base|small|medium|large-v3|turbo")
    p.add_argument("--language", default="en")
    p.add_argument("--pause", type=float, default=0.6, help="mark gaps longer than this (s)")
    a = p.parse_args()

    import whisper  # slow import, keep --help fast

    model = whisper.load_model(a.model)
    r = model.transcribe(a.video, word_timestamps=True, language=a.language, verbose=False)

    words, segments = [], []
    for s in r["segments"]:
        segments.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "text": s["text"].strip()})
        for w in s.get("words", []):
            words.append({"w": w["word"].strip(), "start": round(w["start"], 3), "end": round(w["end"], 3)})

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"words": words, "segments": segments}, f, indent=1)

    out = sys.stdout
    prev_end = 0.0
    for s in segments:
        gap = s["start"] - prev_end
        if gap > a.pause:
            out.write(f"            -- pause {gap:.2f}s --\n")
        flag = "   <<< RETAKE CUE" if CUES.search(s["text"]) else ""
        out.write(f"[{s['start']:7.2f} - {s['end']:7.2f}] {s['text']}{flag}\n")
        prev_end = s["end"]
    out.write(f"\n{len(words)} words, {len(segments)} segments -> {a.out}\n")


if __name__ == "__main__":
    main()
