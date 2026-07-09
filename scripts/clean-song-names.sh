#!/usr/bin/env bash

SONGS_DIR="$(cd "$(dirname "$0")/../songs" && pwd)"

shopt -s nullglob

for file in "$SONGS_DIR"/tagmp3_*.mp3; do
  filename="$(basename "$file")"
  newname="${filename#tagmp3_}"

  mv -n "$file" "$SONGS_DIR/$newname"
  echo "Renamed: $filename -> $newname"
done