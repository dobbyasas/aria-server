#!/usr/bin/env python3
from pathlib import Path
from mutagen.id3 import ID3, error

BASE_DIR = Path(__file__).resolve().parent.parent
SONGS_DIR = BASE_DIR / "songs"

bad = []

for file in sorted(SONGS_DIR.glob("*.mp3")):
    try:
        tags = ID3(file)
    except error:
        continue

    album = tags.get("TALB")
    artist = tags.get("TPE1")
    album_artist = tags.get("TPE2")
    year = tags.get("TDRC")

    album_text = str(album.text[0]) if album and album.text else ""
    artist_text = str(artist.text[0]) if artist and artist.text else ""
    album_artist_text = str(album_artist.text[0]) if album_artist and album_artist.text else ""
    year_text = str(year.text[0]) if year and year.text else ""

    if album_text == "Jar" and artist_text == "Superheaven":
        bad.append((file.name, album_text, artist_text, album_artist_text, year_text))

print(f"Found {len(bad)} files tagged as Jar / Superheaven:\n")

for name, album, artist, album_artist, year in bad:
    print(f"{name} | album={album} | artist={artist} | album_artist={album_artist} | year={year}")
