#!/bin/sh
# generatePlaceholder.sh - Create a placeholder video for skipped YouTube content
# Uses ImageMagick (magick) to render text onto a gradient image, then ffmpeg to encode.
#
# Usage: ./generatePlaceholder.sh <reason> <url> <output_path> [title]
#   reason:      age-restricted | members-only | unavailable | errored
#   url:         YouTube URL to display on the placeholder
#   output_path: Full path for the output .mp4 file
#   title:       Optional video title to display

FONT="/System/Library/Fonts/Avenir Next.ttc"
FONT_FALLBACK="/System/Library/Fonts/Menlo.ttc"
TMPIMG="/tmp/placeholder_$$.png"

reason="$1"
url="$2"
outfile="$3"
title="${4:-}"

if [ -z "$reason" ] || [ -z "$url" ] || [ -z "$outfile" ]; then
  echo "Usage: $0 <reason> <url> <output_path> [title]"
  exit 1
fi

# Pick best available font
USE_FONT="$FONT"
[ ! -f "$FONT" ] && USE_FONT="$FONT_FALLBACK"

# Build body text based on reason
case "$reason" in
  age-restricted)
    header="AGE-RESTRICTED"
    body="YouTube gates this video behind age verification —
which means handing over a Google account
for them to track and eventually ban.

So we let them have this one.

Every other video? Downloaded. Archived. Ours.
Hosted on a self-owned, self-democratized server.
But this one fight we don't pick.
This one small morsel — they can keep it.

Watch it on YouTube or grab it manually.
Automation has its limits."
    ;;
  members-only)
    header="MEMBERS ONLY"
    body="This video is locked behind a paid channel membership.
YouTube won't serve it without an active subscription
and we're not in the business of paying per channel.

You can join the channel on YouTube
or watch it there directly."
    ;;
  unavailable)
    header="UNAVAILABLE"
    body="This video has been made private, deleted,
or region-locked by the uploader.
YouTube is no longer serving it to anyone.

It may still be accessible directly on YouTube
if the status changes."
    ;;
  *)
    header="COULD NOT DOWNLOAD"
    body="YouTube didn't return metadata for this video.
This can happen with premieres, live streams,
or temporary outages on their end.

Try watching it directly on YouTube."
    ;;
esac

# Render gradient background + text with ImageMagick
# Layout (top to bottom): title → header → body → url → signature
if [ -n "$title" ]; then
  magick -size 1920x1080 radial-gradient:'#12122a'-'#060609' \
    -font "$USE_FONT" \
    -fill white    -pointsize 36 -gravity north  -annotate +0+100 "$title" \
    -fill '#777777' -pointsize 20 -gravity north  -annotate +0+155 "$header" \
    -fill '#C0C0C0' -pointsize 22 -interline-spacing 6 -gravity center -annotate +0-20 "$body" \
    -fill '#6699FF' -pointsize 22 -gravity south  -annotate +0+130 "$url" \
    -fill '#444444' -pointsize 18 -gravity south  -annotate +0+90  "—Agnos" \
    "$TMPIMG" 2>/dev/null
else
  magick -size 1920x1080 radial-gradient:'#12122a'-'#060609' \
    -font "$USE_FONT" \
    -fill '#777777' -pointsize 22 -gravity north  -annotate +0+120 "$header" \
    -fill '#C0C0C0' -pointsize 22 -interline-spacing 6 -gravity center -annotate +0-20 "$body" \
    -fill '#6699FF' -pointsize 22 -gravity south  -annotate +0+130 "$url" \
    -fill '#444444' -pointsize 18 -gravity south  -annotate +0+90  "—Agnos" \
    "$TMPIMG" 2>/dev/null
fi

if [ ! -f "$TMPIMG" ]; then
  echo "  WARN: magick failed to create placeholder image"
  exit 1
fi

# Encode as 9-second video
ffmpeg -y -loop 1 -i "$TMPIMG" \
  -f lavfi -i "anullsrc=r=44100:cl=stereo" \
  -t 9 -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 64k -shortest -movflags +faststart \
  "$outfile" -loglevel quiet 2>/dev/null

rm -f "$TMPIMG"

if [ -f "$outfile" ]; then
  exit 0
else
  echo "  WARN: ffmpeg failed to create placeholder video"
  exit 1
fi
