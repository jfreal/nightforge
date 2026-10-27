---
name: short-form-video-edit
description: Edit one raw phone take into a 15-60 s vertical short (TikTok, Reels, Shorts) with Whisper word timings, face-framed jump cuts, one-word-at-a-time captions and app screenshots placed in the empty part of the frame, all driven by a reviewable TSV cut list and rendered with ffmpeg. Use when the user wants to cut, trim or edit talking-head footage into a short, vertical or 9:16 video, make a TikTok/Reel/Short/ad from a recording, add word-by-word captions, or drop retakes and "cut that out" moments.
---

# Short-form video edit

Turns one continuous phone take into a vertical short. The edit lives in a
plain TSV file, so the user can fix timing by changing a number instead of
re-editing. Adapted from Brooke Lacey's workflow on ChatPRD's *How I AI*.

Scripts live in `scripts/` under this skill's base directory (Claude Code prints
it when the skill loads; call it `$SKILL_DIR`). All three are Python with
`argparse --help`.

| Script | Does | Writes |
|---|---|---|
| `transcribe.py` | Whisper with word timestamps; prints a transcript with pauses and retake cues flagged | `words.json` |
| `faces.py` | OpenCV face track every 0.25 s; says which side of frame is empty | `faces.json` |
| `render.py` | TSV -> crop/zoom per cut, jump cuts, captions, graphics, sync check, contact sheet | `out.mp4`, `out-sheet.png` |

## Needs

`ffmpeg`/`ffprobe` with libx264 and libass (any recent version; 7.0 or newer
for very long cut lists, which pass the filter graph as a file), Python with `openai-whisper` and
`opencv-python`. On John's PC: ffmpeg 8 via winget, and `python` (3.14) has both
packages. Run the scripts from Git Bash. Whisper on CPU: `base` takes ~10 s per
minute of footage, `small` (default) is more accurate and slower.

## Workflow

Work in a folder next to the footage (e.g. `<video-name>-edit/`). Never write
into the user's original file.

1. **Ask only what you can't infer:** target length (default 15 s for an ad,
   30-45 s otherwise), the graphic to show (screenshot path, or "take one from the
   simulator / site"), and the one point the video must land. Probe the clip
   with `ffprobe` and tell the user its size, length and fps.
2. **Analyse.**

   ```
   python "$SKILL_DIR/scripts/transcribe.py" raw.mov --out words.json
   python "$SKILL_DIR/scripts/faces.py" raw.mov --out faces.json
   ```

3. **Pick the cuts.** This is the judgment part; do it yourself from
   `words.json`, not with a script.
   - When a line is said twice, keep the **later** take unless the earlier one
     is clearly better. Drop every retake cue ("cut that out", "start over")
     and the take it refers to.
   - Open on the hook: the first cut should state the problem or the payoff in
     under 2 s. End on the call to action.
   - Cut on word boundaries: `start = first word start - 0.06`,
     `end = last word end + 0.10`. Never start or end inside a word. Remove
     dead air, "um", "so, yeah".
   - Vary shot size so jump cuts read as intent: about `wide` 40%, `mid` 35%,
     `close` 25%. Use `close` for punchlines and the CTA. Don't use the same size
     twice in a row.
   - Put a graphic on cuts where the words talk about the product. Sum the cut
     lengths and stay within the target length.
4. **Write `cuts.tsv`** (tabs, one row per cut):

   ```
   start	end	shot	label	overlay
   5.76	7.30	wide	hook	-
   7.30	9.00	close	payoff	yes
   9.80	11.40	mid	speed	shots/pricing.png
   ```

   `overlay` is `-`, `yes` (the `--overlay` image) or an image path. Times may be
   seconds or `m:ss.xx`.
5. **Human checkpoint.** Show the user the cut list as a table with each cut's
   spoken words and the total length. Ask them to approve or change it before
   the full render. This is the cheapest place to fix pacing.
6. **Render.**

   ```
   python "$SKILL_DIR/scripts/render.py" --video raw.mov --cuts cuts.tsv \
     --words words.json --faces faces.json --overlay app.png --out short.mp4
   ```

   Options: `--size 1080x1920` (default), `--fps 30`, `--upper` (UPPERCASE
   captions), `--overlay-side left|right`, `--no-captions`. Each cut prints its
   crop and graphic box; `TIGHT` means the graphic had little room.
7. **Check it yourself before reporting.** Read `short-sheet.png` (one frame per
   cut): the face is not cut off, the graphic does not cover the face, captions
   sit in the lower third. Read the sync line: A/V diff under 40 ms is fine
   (AAC frame size). Then for each cut, extract 0.2 s of audio around the joins
   if a word may be clipped. Fix by editing `cuts.tsv` and rendering again.
8. **Report** the output path, length, number of cuts, and anything you could
   not check by looking (how the cadence *feels*). Tell the user to watch it once
   with sound; humour and timing are their call.

## If it goes wrong

| Problem | Fix |
|---|---|
| Word clipped or awkward pause at a join | Move that row's `start`/`end` by 0.05-0.1 s in `cuts.tsv` and re-render |
| Graphic covers the face or is tiny | `--overlay-side` the other way, use `wide`/`mid` for that cut, or a taller/narrower image |
| Face not found (`no face`) | Crop centres; if framing is wrong, the speaker may be in profile. Use `wide` for those cuts |
| Captions missing | ffmpeg lacks libass, or the font is absent: check `ffmpeg -filters` lists `subtitles` |
| Whisper mishears names | Use `--model medium`; caption text comes from `words.json`, so fix spellings there and re-render |
| Clip is sideways | Phone rotation tag. ffmpeg applies it, and both scripts tell OpenCV to apply it too, so analysis and render see the same frame. If it is still wrong, bake the rotation in first (`ffmpeg -i raw.mov -c:a copy fixed.mp4`) and run every step on `fixed.mp4` |

## Filming tips to pass on

One continuous take. Repeat a line as many times as needed and say "cut that
out" after a bad one. Leave empty space (a blank wall) on one side of the frame
so graphics have somewhere to go.
