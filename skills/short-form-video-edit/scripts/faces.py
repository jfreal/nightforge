"""Track the speaker's face so each cut can be framed around it.

Samples the video every --step seconds with OpenCV's Haar face detector and
writes {"width", "height", "duration", "samples": [{"t", "x", "y", "w", "h"}]}
with the box in source pixels (largest face per sample; missing samples are
simply absent). Prints a short summary: face found rate and which side of the
frame is empty (where graphics can go).
"""
import argparse
import json

import cv2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out", required=True)
    p.add_argument("--step", type=float, default=0.25)
    a = p.parse_args()

    cap = cv2.VideoCapture(a.video)
    # match ffmpeg, which applies phone rotation tags; OpenCV 4.11+ no longer does by default
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {a.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    samples, width, height, t = [], 0, 0, 0.0
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        height, width = frame.shape[:2]
        scale = 640 / max(width, height)
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
        s = scale if scale < 1 else 1.0
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        faces = cascade.detectMultiScale(gray, 1.1, 6, minSize=(40, 40))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            samples.append({"t": round(t, 2), "x": int(x / s), "y": int(y / s), "w": int(w / s), "h": int(h / s)})
        t += a.step

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"width": width, "height": height, "duration": round(duration, 3), "samples": samples}, f)

    total = max(1, int(duration / a.step))
    print(f"{width}x{height}, {duration:.1f}s, face in {len(samples)}/{total} samples -> {a.out}")
    if samples:
        cx = sorted(s["x"] + s["w"] / 2 for s in samples)[len(samples) // 2] / width
        side = "right" if cx < 0.5 else "left"
        print(f"median face centre at {cx:.0%} of width; emptier side: {side}")
    else:
        print("no face found: render will centre the crop")


if __name__ == "__main__":
    main()
