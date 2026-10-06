#!/usr/bin/env bash
# Prepend an animated logo intro to a video and transition into it with ffmpeg xfade.
#
#   logo-intro.sh --logo logo-white.png --video in.mp4 --out out.mp4 [options]
#
# Options (defaults in brackets):
#   --title TEXT        wordmark under the logo; "" for none           [""]
#   --subtitle TEXT     smaller line under the title (the post's title)  [""]
#   --bg HEX            intro background colour                         [0b1316]
#   --glow HEX          colour of the blurred halo behind the logo      [33bff2]
#   --transition NAME   any xfade transition (circleopen, wipeleft,
#                       slideup, pixelize, radial, dissolve, ...)      [circleopen]
#   --intro SECONDS     intro length, transition included               [3.8]
#   --xfade SECONDS     transition length                               [1.0]
#   --logo-scale FRAC   logo width as a fraction of the video width     [0.30]
#   --font PATH         TTF for --title/--subtitle   [a bold system font, found per OS]
#   --preview SECONDS   render only the first N seconds and write a
#                       six-frame contact sheet next to --out           [off]
#
# Needs ffmpeg/ffprobe (with libx264) and python on PATH. Runs in Git Bash, macOS, Linux.
set -euo pipefail

TITLE=""; SUB=""; BG=0b1316; GLOW=33bff2; TRANS=circleopen; D=3.8; XF=1.0; SCALE=0.30
FONT=""; PREVIEW=""; LOGO=""; VID=""; OUT=""
while [ $# -gt 0 ]; do
  # Every option takes a value; refuse to swallow the next flag as one.
  if [ $# -lt 2 ] || [ "${2#--}" != "$2" ]; then echo "missing value for $1" >&2; exit 2; fi
  case "$1" in
    --logo) LOGO=$2;; --video) VID=$2;; --out) OUT=$2;; --title) TITLE=$2;; --subtitle) SUB=$2;;
    --bg) BG=${2#\#};; --glow) GLOW=${2#\#};; --transition) TRANS=$2;;
    --intro) D=$2;; --xfade) XF=$2;; --logo-scale) SCALE=$2;; --font) FONT=$2;;
    --preview) PREVIEW=$2;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
  shift 2
done
[ -n "$LOGO" ] && [ -n "$VID" ] && [ -n "$OUT" ] || { echo "need --logo, --video and --out" >&2; exit 2; }

# These values are spliced into python -c and the filter graph, so they must be plain.
num='^[0-9]+([.][0-9]+)?$'
for v in "$D" "$XF" "$SCALE" ${PREVIEW:+"$PREVIEW"}; do
  [[ $v =~ $num ]] || { echo "not a number: $v" >&2; exit 2; }
done
for v in "$BG" "$GLOW"; do
  [[ $v =~ ^[0-9a-fA-F]{6}$ ]] || { echo "not a 6-digit hex colour: $v" >&2; exit 2; }
done
[[ $TRANS =~ ^[a-z]+$ ]] || { echo "not an xfade transition name: $TRANS" >&2; exit 2; }

if [ -n "$TITLE$SUB" ] && [ -z "$FONT" ]; then
  for f in C:/Windows/Fonts/segoeuib.ttf C:/Windows/Fonts/arialbd.ttf            "/System/Library/Fonts/Supplementary/Arial Bold.ttf" "/Library/Fonts/Arial Bold.ttf"            /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf /usr/share/fonts/TTF/DejaVuSans-Bold.ttf; do
    [ -f "$f" ] && { FONT=$f; break; }
  done
  [ -z "$FONT" ] && command -v fc-match >/dev/null && FONT=$(fc-match -f '%{file}' 'sans:bold' || true)
  [ -n "$FONT" ] || { echo "no bold font found; pass --font /path/to/font.ttf" >&2; exit 2; }
fi

# Windows python and ffprobe end lines with CRLF; a stray \r breaks the next python -c.
py() { python -c "$1" | tr -d '\r'; }

# Match the intro canvas to the source so xfade gets identical size, rate and timebase.
PROBE=$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,avg_frame_rate -of csv=p=0 "$VID" | tr -d '\r') \
  || { echo "ffprobe could not read $VID" >&2; exit 1; }
IFS=, read -r W H RFR AFR <<<"$PROBE" || true
[[ ${W:-} =~ ^[0-9]+$ && ${H:-} =~ ^[0-9]+$ ]] || { echo "no video stream in $VID" >&2; exit 1; }
# r_frame_rate first, avg_frame_rate if that is 0/0 or missing. No silent default.
FPS=""
for r in "${RFR:-}" "${AFR:-}"; do
  if [[ $r =~ ^([1-9][0-9]*)/([1-9][0-9]*)$ ]]; then
    FPS=$(py "print(max(1,round(${BASH_REMATCH[1]}/${BASH_REMATCH[2]})))"); break
  fi
done
[ -n "$FPS" ] || { echo "no usable frame rate in $VID (r=${RFR:-} avg=${AFR:-})" >&2; exit 1; }
HAS_AUDIO=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$VID" | tr -d '\r' | head -1)

OFF=$(py "print($D-$XF)")
LOGO_W=$(py "print(int($W*$SCALE/2)*2)")
# Glow tint: colorchannelmixer turns the white logo into the glow colour (alpha untouched).
read -r GR GG GB < <(py "h='$GLOW';print(*[round(int(h[i:i+2],16)/255,3) for i in (0,2,4)])")
FS=$(py "print(round($H*0.06))")
LOGO_DY=$(py "print(round($H*0.055))")
TEXT_Y=$(py "print(round($H*0.20))")

# drawtext needs colons escaped. expansion=none stops it reading % as a template code.
FONT_ESC=${FONT//:/\\:}
# A straight ' would end the quoted value, so it becomes a typographic one.
esc() { printf '%s' "$1" | sed "s/:/\\\\:/g; s/%/\\\\%/g; s/'/’/g"; }
# fade(start): 0 -> 1 over 0.6 s. Every expression with a comma stays single-quoted.
fade() { echo "min(1,max(0,(t-$1)/0.6))"; }
TEXT=""
if [ -n "$TITLE" ]; then
  TEXT=",drawtext=expansion=none:fontfile='$FONT_ESC':text='$(esc "$TITLE")':fontsize=$FS:fontcolor=white:x=(w-text_w)/2:y='h/2+$TEXT_Y-12*(1-$(fade 0.9))':alpha='$(fade 0.9)'"
fi
if [ -n "$SUB" ]; then
  SUB_FS=$(py "print(round($H*0.036))")
  HAS_TITLE=0; [ -n "$TITLE" ] && HAS_TITLE=1
  SUB_Y=$(py "print(round($H*0.20+$FS*1.45*$HAS_TITLE))")
  TEXT="$TEXT,drawtext=expansion=none:fontfile='$FONT_ESC':text='$(esc "$SUB")':fontsize=$SUB_FS:fontcolor=white@0.8:x=(w-text_w)/2:y='h/2+$SUB_Y-10*(1-$(fade 1.3))':alpha='$(fade 1.3)'"
fi

FC="color=c=0x$BG:s=${W}x${H}:r=$FPS:d=$D,format=rgba,vignette=PI/4[bg];
[0:v]format=rgba,pad=iw*1.26:ih*1.4:(ow-iw)/2:(oh-ih)/2:color=black@0,split[a][b];
[b]colorchannelmixer=rr=$GR:rg=0:rb=0:gr=0:gg=$GG:gb=0:br=0:bg=0:bb=$GB,gblur=sigma=40,colorlevels=aimax=0.6,split[g1][g2];
[g1][g2]overlay=format=auto[glow];
[glow][a]overlay=0:0:format=auto,
 scale=w='trunc($LOGO_W*1.26*(0.82+0.18*(1-pow(1-min(1,t/1.1),3)))/2)*2':h=-2:eval=frame,
 fade=t=in:st=0.15:d=0.9:alpha=1[logo];
[bg][logo]overlay=x=(W-w)/2:y=(H-h)/2-$LOGO_DY:format=auto:eval=frame$TEXT,
 format=yuv420p,setsar=1,settb=AVTB[intro];
[1:v]fps=$FPS,scale=$W:$H,format=yuv420p,setsar=1,settb=AVTB[main];
[intro][main]xfade=transition=$TRANS:duration=$XF:offset=$OFF,format=yuv420p[v]"

MAPS=(-map "[v]")
if [ -n "$HAS_AUDIO" ]; then
  FC="$FC;
[1:a]adelay=$(py "print(int($OFF*1000))"):all=1,afade=t=in:st=$OFF:d=0.6[aud]"
  MAPS+=(-map "[aud]" -c:a aac -b:a 192k)
fi

LIMIT=()
[ -n "$PREVIEW" ] && LIMIT=(-t "$PREVIEW")

# xfade quietly promotes to yuv444p, which Windows players reject as "invalid encoding
# settings". The format=yuv420p after xfade plus -pix_fmt/-profile below pin it back.
ffmpeg -v warning -stats -y -loop 1 -framerate "$FPS" -t "$D" -i "$LOGO" -i "$VID" \
  -filter_complex "$FC" "${MAPS[@]}" \
  -c:v libx264 -pix_fmt yuv420p -profile:v high -crf 18 -preset medium \
  -movflags +faststart "${LIMIT[@]}" "$OUT"

ffprobe -v error -show_entries format=duration,size:stream=codec_name,profile,pix_fmt \
  -of compact "$OUT"

if [ -n "$PREVIEW" ]; then
  SHEET="${OUT%.*}-frames.png"
  ARGS=(); i=0
  # Clamp to the rendered length, or a short --preview asks for frames past its end.
  for t in $(py "d,o,x,p=$D,$OFF,$XF,$PREVIEW;print(*[round(min(t,p-0.05),3) for t in (1.5,o+0.1,o+x*0.3,o+x*0.6,d+0.1,d+1)])"); do
    ARGS+=(-ss "$t" -i "$OUT"); i=$((i+1))
  done
  ffmpeg -v error -y "${ARGS[@]}" -filter_complex \
    "[0]scale=480:-2[p0];[1]scale=480:-2[p1];[2]scale=480:-2[p2];[3]scale=480:-2[p3];[4]scale=480:-2[p4];[5]scale=480:-2[p5];[p0][p1][p2]hstack=3[t];[p3][p4][p5]hstack=3[u];[t][u]vstack" \
    -frames:v 1 "$SHEET"
  echo "contact sheet: $SHEET"
fi
