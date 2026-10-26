"""Render a vertical short from a TSV cut list.

cuts.tsv columns (tab separated, header row optional, '#' lines ignored):
    start   end   shot   label   overlay
start/end are source seconds (or m:ss.xx). shot is wide|mid|close.
overlay is '-' (none), 'yes' (use --overlay image) or a path to an image.

Each cut is cropped to the output aspect around the tracked face, zoomed by
shot size, and joined with hard jump cuts. Audio is cut by the same filter
graph as video, so A/V offset stays zero. Captions are one word at a time,
retimed onto the new timeline. Writes <out> and <out>-sheet.png (one frame
per cut) and prints a sync check.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import cv2

ZOOM = {"wide": 1.0, "mid": 0.78, "close": 0.6}


def t2s(v):
    v = v.strip()
    if ":" in v:
        parts = [float(x) for x in v.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return float(v)


def read_cuts(path):
    cuts = []
    # utf-8-sig: Windows editors may write a BOM onto the header cell
    with open(path, encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.reader(f, delimiter="\t"), 1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if row[0].strip().lower() == "start":
                continue
            if len(row) < 2:
                raise SystemExit(f"{path} line {n}: need tab-separated columns (spaces used?): {row[0]!r}")
            row += [""] * (5 - len(row))
            start, end = t2s(row[0]), t2s(row[1])
            if end <= start:
                raise SystemExit(f"cut ends before it starts: {row}")
            shot = (row[2].strip() or "wide").lower()
            if shot not in ZOOM:
                raise SystemExit(f"unknown shot '{shot}' (use wide|mid|close)")
            cuts.append({"start": start, "end": end, "shot": shot, "label": row[3].strip(),
                         "overlay": row[4].strip() or "-"})
    if not cuts:
        raise SystemExit("no cuts in TSV")
    return cuts


def even(x):
    return max(2, int(x) // 2 * 2)


def face_in(faces, s, e):
    if not faces:
        return None
    hits = [f for f in faces["samples"] if s <= f["t"] <= e] or \
        sorted(faces["samples"], key=lambda f: abs(f["t"] - (s + e) / 2))[:1]
    if not hits:
        return None
    med = lambda k: sorted(h[k] for h in hits)[len(hits) // 2]
    return {k: med(k) for k in ("x", "y", "w", "h")}


def plan_crop(cut, face, sw, sh, W, H, want_room):
    A = W / H
    cw, ch = (sh * A, sh) if sw / sh > A else (sw, sw / A)
    z = ZOOM[cut["shot"]]
    cw, ch = even(cw * z), even(ch * z)
    if face:
        fx, fy = face["x"] + face["w"] / 2, face["y"] + face["h"] / 2
        # leave room beside the face when a graphic goes in this cut
        pos = 0.5 if not want_room else (0.33 if fx < sw / 2 else 0.67)
        x0, y0 = fx - cw * pos, fy - ch * (0.40 if cut["shot"] == "close" else 0.35)
    else:
        x0, y0 = (sw - cw) / 2, (sh - ch) / 2
    x0 = int(min(max(0, x0), sw - cw)) // 2 * 2
    y0 = int(min(max(0, y0), sh - ch)) // 2 * 2
    return cw, ch, x0, y0


def plan_overlay(img, face, crop, W, H, side):
    cw, ch, x0, y0 = crop
    im = cv2.imread(img, cv2.IMREAD_UNCHANGED)
    if im is None:
        raise SystemExit(f"cannot read overlay image: {img}")
    ih, iw = im.shape[:2]
    k, m = W / cw, W * 0.04
    if face:
        fl = (face["x"] - x0) * k
        fr = (face["x"] + face["w"] - x0) * k
        fcy = (face["y"] + face["h"] / 2 - y0) * k
        left, right = (m, fl - m), (fr + m, W - m)
    else:
        fcy, left, right = H * 0.35, (m, W / 2 - m), (W / 2 + m, W - m)
    if side == "auto":
        side = "left" if left[1] - left[0] > right[1] - right[0] else "right"
    lo, hi = left if side == "left" else right
    room = max(0, hi - lo)
    ow = min(room, W * 0.5)
    oh = ow * ih / iw
    if oh > H * 0.4:
        oh, ow = H * 0.4, H * 0.4 * iw / ih
    ow, oh = even(ow), even(oh)
    x = lo if side == "left" else hi - ow
    y = min(max(H * 0.06, fcy - oh / 2), H * 0.65 - oh)
    warn = room < W * 0.18
    return ow, oh, int(x), int(y), warn


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_captions(path, words, cuts, W, H, upper):
    events, off = [], 0.0
    for c in cuts:
        for w in words:
            mid = (w["start"] + w["end"]) / 2
            if c["start"] <= mid <= c["end"]:
                txt = re.sub(r"^[^\w']+|[^\w'%$!?]+$", "", w["w"])
                if txt:
                    s = off + max(0.0, w["start"] - c["start"])
                    e = off + min(c["end"], w["end"]) - c["start"]
                    events.append([s, e, txt.upper() if upper else txt])
        off += c["end"] - c["start"]
    for i, ev in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else ev[1] + 0.3
        ev[1] = max(ev[0] + 0.08, min(nxt, ev[0] + 1.0) if nxt - ev[1] < 1.0 else ev[1] + 0.2)
    fs, mv = int(H * 0.055), int(H * 0.22)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,Arial,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{max(4, fs // 14)},0,2,40,40,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        for s, e, t in events:
            f.write(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Word,,0,0,0,,{t}\n")
    return len(events)


def has_audio(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", video], capture_output=True, text=True).stdout
    return bool(out.strip())


def stream_durations(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", path], capture_output=True, text=True).stdout
    return {s["codec_type"]: float(s.get("duration", 0)) for s in json.loads(out)["streams"]}


def contact_sheet(out, cuts, W, H, sheet):
    tiles, off = [], 0.0
    for c in cuts:
        L = c["end"] - c["start"]
        cap = cv2.VideoCapture(out)
        cap.set(cv2.CAP_PROP_POS_MSEC, (off + L / 2) * 1000)
        ok, fr = cap.read()
        cap.release()
        off += L
        if not ok:
            continue
        fr = cv2.resize(fr, (270, int(270 * H / W)))
        cv2.rectangle(fr, (0, 0), (150, 38), (0, 0, 0), -1)
        cv2.putText(fr, f"{len(tiles) + 1} {c['shot']}", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        tiles.append(fr)
    if not tiles:
        return
    cols = min(6, len(tiles))
    while len(tiles) % cols:
        tiles.append(tiles[0] * 0)
    rows = [cv2.hconcat(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(sheet, cv2.vconcat(rows))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--cuts", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--words", help="words JSON from transcribe.py (captions)")
    p.add_argument("--faces", help="faces JSON from faces.py (framing, graphic room)")
    p.add_argument("--overlay", help="default image for overlay=yes rows")
    p.add_argument("--overlay-side", default="auto", choices=["auto", "left", "right"])
    p.add_argument("--size", default="1080x1920")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--upper", action="store_true", help="UPPERCASE captions")
    p.add_argument("--crf", type=int, default=18)
    a = p.parse_args()

    W, H = (int(x) for x in a.size.lower().split("x"))
    video, out = os.path.abspath(a.video), os.path.abspath(a.out)
    cuts = read_cuts(a.cuts)
    faces = json.load(open(a.faces, encoding="utf-8")) if a.faces else None
    cap = cv2.VideoCapture(video)
    ok, fr = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"cannot read {video}")
    sh, sw = fr.shape[:2]
    audio = has_audio(video)

    inputs, chains, vlabels, alabels = ["-i", video], [], [], []
    n_in = 1
    for i, c in enumerate(cuts):
        L = c["end"] - c["start"]
        img = None if c["overlay"] in ("-", "no", "") else (a.overlay if c["overlay"] == "yes" else c["overlay"])
        if c["overlay"] == "yes" and not a.overlay:
            raise SystemExit(f"cut {i + 1} wants overlay=yes but --overlay not given")
        face = face_in(faces, c["start"], c["end"])
        crop = plan_crop(c, face, sw, sh, W, H, bool(img))
        cw, ch, x0, y0 = crop
        v = (f"[0:v]trim=start={c['start']:.3f}:end={c['end']:.3f},setpts=PTS-STARTPTS,"
             f"crop={cw}:{ch}:{x0}:{y0},scale={W}:{H}:flags=lanczos,fps={a.fps},setsar=1,format=yuv420p")
        note = ""
        if img:
            ow, oh, ox, oy, warn = plan_overlay(img, face, crop, W, H, a.overlay_side)
            inputs += ["-loop", "1", "-t", f"{L:.3f}", "-i", os.path.abspath(img)]
            chains.append(f"{v}[b{i}]")
            chains.append(f"[{n_in}:v]scale={ow}:{oh},format=rgba,fade=in:st=0:d=0.2:alpha=1[g{i}]")
            chains.append(f"[b{i}][g{i}]overlay={ox}:{oy}:shortest=1[v{i}]")
            n_in += 1
            note = f"  graphic {ow}x{oh} at {ox},{oy}" + ("  (TIGHT: little empty space)" if warn else "")
        else:
            chains.append(f"{v}[v{i}]")
        vlabels.append(f"[v{i}]")
        if audio:
            fd = min(0.015, L / 4)
            chains.append(f"[0:a]atrim=start={c['start']:.3f}:end={c['end']:.3f},asetpts=PTS-STARTPTS,"
                          f"aresample=48000,afade=t=in:d={fd:.3f},afade=t=out:st={L - fd:.3f}:d={fd:.3f}[a{i}]")
            alabels.append(f"[a{i}]")
        print(f"cut {i + 1:2d} {c['start']:7.2f}-{c['end']:7.2f} ({L:4.2f}s) {c['shot']:5s} "
              f"crop {cw}x{ch}+{x0}+{y0}{'' if face else ' (no face)'}{note}  {c['label']}")

    pairs = "".join(v + (al if audio else "") for v, al in zip(vlabels, alabels or vlabels))
    chains.append(f"{pairs}concat=n={len(cuts)}:v=1:a={1 if audio else 0}[vc]" + ("[ac]" if audio else ""))

    tmp = tempfile.mkdtemp(prefix="sfve-")
    vout = "[vc]"
    if a.words and not a.no_captions:
        words = json.load(open(a.words, encoding="utf-8"))["words"]
        n = write_captions(os.path.join(tmp, "captions.ass"), words, cuts, W, H, a.upper)
        chains.append("[vc]subtitles=captions.ass[vs]")
        vout = "[vs]"
        print(f"captions: {n} words")
    graph = ";".join(chains)
    if len(graph) < 24000:
        # inline works on every ffmpeg version and fits the Windows command-line limit
        fc = ["-filter_complex", graph]
    else:
        # long cut lists: read the graph from a file (-/ prefix needs ffmpeg 7.0 or newer)
        with open(os.path.join(tmp, "graph.txt"), "w", encoding="utf-8") as f:
            f.write(graph)
        fc = ["-/filter_complex", "graph.txt"]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats", *inputs, *fc, "-map", vout]
    if audio:
        cmd += ["-map", "[ac]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(a.crf), "-r", str(a.fps),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    # cwd=tmp so the subtitles filter gets a bare filename (no Windows path escaping)
    r = subprocess.run(cmd, cwd=tmp)
    if r.returncode:
        raise SystemExit(f"ffmpeg failed; captions kept in {tmp}")

    total = sum(c["end"] - c["start"] for c in cuts)
    d = stream_durations(out)
    print(f"\nwrote {out}\nplanned {total:.2f}s; video {d.get('video', 0):.2f}s"
          + (f", audio {d.get('audio', 0):.2f}s, A/V diff {abs(d.get('video', 0) - d.get('audio', 0)) * 1000:.0f} ms"
             if audio else ", no audio"))
    sheet = os.path.splitext(out)[0] + "-sheet.png"
    contact_sheet(out, cuts, W, H, sheet)
    print(f"contact sheet: {sheet}")


if __name__ == "__main__":
    sys.exit(main())
