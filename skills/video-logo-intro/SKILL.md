---
name: video-logo-intro
description: Put an animated logo opening on a video (logo fades and scales in with a glow, optional wordmark, then an ffmpeg xfade transition such as circleopen into the footage). Use when the user wants an intro, opener, logo sting, bumper, or "cool transition" at the start of a screen recording or clip, or invokes /video-logo-intro.
---

# Video logo intro

Builds a short branded opening with ffmpeg and joins it to the user's video in one pass.
`scripts/logo-intro.sh` does the work. You pick the inputs, preview, look, then render.

The default intro runs 3.8 s:

| Time | What happens |
|---|---|
| 0.15 to 1.05 s | logo fades in and eases from 82% to 100% size, a tinted blur glowing behind it |
| 0.9 to 1.5 s | wordmark (`--title`) fades in under the logo and drifts up 12 px |
| 2.8 to 3.8 s | `xfade` transition (default `circleopen`) reveals the video |

The source audio starts at the transition with a 0.6 s fade-in. The intro itself is silent.

## Workflow

1. **Check tools.** You need `ffmpeg` and `ffprobe` (with libx264) and `python`. On John's PC
   they are installed via winget and the script runs in Git Bash.
2. **Probe the video.** Get its size, frame rate, length, and whether it has audio. The
   script matches the intro canvas to the source automatically, but the user should hear
   the numbers.
3. **Find the logo.** Prefer a transparent PNG in one solid colour. A white logo suits a
   dark intro. Look in the project first (`public/logo*.png`, `public/*.svg`). Read the image
   to check it: a white-on-transparent PNG renders as a blank white square in the image
   viewer, and that is fine. Pull the brand accent colour from the project's CSS
   (`--accent` and friends) for `--glow`.
4. **Grab one frame of the video** (`ffmpeg -ss 0.5 -i in.mp4 -frames:v 1 first.png`) and
   look at it. Choose `--bg` to sit well next to the first shot, so the transition doesn't
   flash.
5. **Preview.** Render only the first few seconds, which also writes a six-frame contact
   sheet (`<out>-frames.png`): mid-intro, start of the transition, two points inside it,
   and two after it. Read the sheet and confirm the logo, text, and transition look right.

   ```bash
   bash scripts/logo-intro.sh --logo public/logo-white.png --video in.mp4 \
     --out "$SCRATCH/preview.mp4" --title "Merge & Tell" --glow 0d7ea4 --preview 6
   ```

6. **Render the full video** with the same flags minus `--preview`. Write it next to the
   source as `<name>_intro.mp4`, never over the original. Then confirm the output with the
   `ffprobe` line the script prints: `profile=High`, `pix_fmt=yuv420p`, and a length of
   source + intro - xfade.
7. **Send the file** (SendUserFile) and list the easy changes: speed, transition, text, an
   outro, a sound effect.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--title` | none | wordmark under the logo |
| `--bg` | `0b1316` | intro background, with a vignette on top |
| `--glow` | `33bff2` | halo colour |
| `--transition` | `circleopen` | any [xfade transition](https://trac.ffmpeg.org/wiki/Xfade): `wipeleft`, `slideup`, `radial`, `pixelize`, `dissolve`, `circlecrop`, `zoomin`, ... |
| `--intro` | `3.8` | seconds, transition included |
| `--xfade` | `1.0` | transition length |
| `--logo-scale` | `0.30` | logo width / video width |
| `--font` | `C:/Windows/Fonts/segoeuib.ttf` | TTF used for `--title` |
| `--preview N` | off | first N seconds plus a contact sheet |

## Gotchas (each cost a round trip once)

- **"Invalid encoding settings" in Windows players.** `xfade` silently promotes to
  `yuv444p`, so x264 writes *High 4:4:4 Predictive*, which the Photos app and most players
  refuse. The script forces `format=yuv420p` after xfade plus `-pix_fmt yuv420p -profile:v
  high`. Keep both if you edit the filter graph. yuv420p is a colour-storage format, not a
  resolution: the output keeps the source's full size.
- **xfade needs identical inputs.** Both streams must share size, frame rate, pixel format,
  SAR, and timebase, hence `fps`, `scale`, `setsar=1`, `settb=AVTB` on both sides.
- **Commas inside filter expressions** must be wrapped in single quotes (`y='...min(1,x)...'`),
  or ffmpeg splits the filter there and reports "No option name near ...".
- **drawtext on Windows** needs the drive colon escaped: `fontfile='C\:/Windows/Fonts/...'`.
- **CRLF.** Windows `python` and `ffprobe` print `\r\n`. A stray `\r` inside a value passed
  to the next `python -c` gives "unterminated string literal". The script strips it.
- **Don't fade the logo out before the transition.** Two fades at once read as a muddy
  dissolve. Leave the logo solid and let the xfade shape do the reveal.
- Rendering 1.5 minutes of 1488x1078 at 60 fps takes a few minutes. Give the Bash call a
  long timeout (600000 ms).

## Bigger asks

ffmpeg handles fades, scales, glows, text, and about 40 transition shapes. Particles, 3D
spins, or a logo that draws itself stroke by stroke need an animation tool. One option is
an HTML/CSS animation recorded with Playwright, then joined with the same xfade step. Say
so up front rather than faking it.
