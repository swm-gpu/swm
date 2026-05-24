#!/usr/bin/env bash
# Re-fetch the three CC0 lofi candidate tracks from Free Music Archive.
# Run this once after cloning if public/music/ is empty. The .mp3 files
# are gitignored to keep repo size down.
#
# Licensing: all three are CC0 1.0 Universal (public domain) by HoliznaCC0.
# No attribution required.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/public/music"
mkdir -p "$DIR"

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"
BASE="https://files.freemusicarchive.org/storage-freemusicarchive-org/tracks"

declare -a TRACKS=(
  "01-vintage:Bki0dtfe4SfgBMxIBMOaXcuePHGCLbaL7QjZAcH4"
  "02-theta-frequency:rSIDyunfJfiKNelwFuwbGKoLj5TO8eHFbdSa1zAb"
  "03-two-hour-delay:ZtIu0scNVo0F82GthYO42i4zvmoyuy9W8Y35NxVT"
)

for entry in "${TRACKS[@]}"; do
  name="${entry%%:*}"
  id="${entry##*:}"
  out="$DIR/$name.mp3"
  if [[ -f "$out" ]]; then
    echo "✓ $name.mp3 (exists)"
    continue
  fi
  echo "▸ downloading $name.mp3"
  curl -sL -A "$UA" -o "$out" "$BASE/$id.mp3"
  echo "✓ $name.mp3"
done

echo ""
echo "Done. Active track is set in src/config/music.ts (MUSIC_TRACK)."
