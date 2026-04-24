#!/bin/bash
# testMediaSpread.sh — populate display cache with real items at varied times
# Items are spread across the last 28h so they expire at different points.
# Run this once; subsequent reportMaker.sh runs naturally persist/expire them.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CACHE="$SCRIPT_DIR/.media_cache"
STATE="$SCRIPT_DIR/.media_state"

# Step 1: reset scan so mediaScan finds everything, but wipe cache so we get fresh added_at
printf 'last_scan=1\n' > "$STATE"
rm -f "$CACHE"

# Step 2: run scan — builds cache with all items, added_at=now
echo "Scanning all library items..."
bash "$SCRIPT_DIR/mediaScan.sh" /tmp/media_scan_items.txt 2>&1

if [ ! -s "$CACHE" ]; then
  echo "Cache empty after scan — nothing found." >&2
  exit 1
fi

now=$(date +%s)
SPREAD_HOURS=28
PER_CAT=2

# Step 3: trim to PER_CAT items per category
tmp_trim=$(mktemp)
for cat in show movie music book manga; do
  grep $'^' "$CACHE" | awk -F'\037' -v c="$cat" '$1==c' | head -"$PER_CAT"
done > "$tmp_trim"
count=$(wc -l < "$tmp_trim" | tr -d ' ')
echo "Trimmed to $count items (${PER_CAT} per category), spreading across ${SPREAD_HOURS}h..."

# Step 4: assign varied added_at spread over 0..SPREAD_HOURS ago
tmp=$(mktemp)
i=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  prefix=$(awk -F'\037' 'BEGIN{OFS="\037"} {print $1,$2,$3,$4}' <<< "$line")
  [ "$count" -gt 1 ] && offset=$(( i * SPREAD_HOURS * 3600 / (count - 1) )) || offset=0
  added_at=$(( now - offset ))
  printf '%s\037%s\n' "$prefix" "$added_at" >> "$tmp"
  i=$(( i + 1 ))
done < "$tmp_trim"
rm -f "$tmp_trim"
mv "$tmp" "$CACHE"
echo "Done. Items will expire gradually over the next ${SPREAD_HOURS}h."

# Step 4: rebuild report
sh "$SCRIPT_DIR/reportMaker.sh"
