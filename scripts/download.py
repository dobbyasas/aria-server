#!/usr/bin/env python3
import re
import subprocess
from pathlib import Path

from mutagen.id3 import (
    ID3,
    TIT2,
    TPE1,
    TPE2,
    TALB,
    TDRC,
    TRCK,
    APIC,
    error,
)

BASE_DIR = Path(__file__).resolve().parent.parent
SONGS_DIR = BASE_DIR / "songs"
SONGS_DIR.mkdir(exist_ok=True)


def get_track_number(path: Path) -> int | None:
    # Expected: "01 - Song Name.mp3"
    match = re.match(r"^(\d+)\s+-\s+", path.name)
    if not match:
        return None
    return int(match.group(1))


def get_title_from_filename(path: Path) -> str:
    # "01 - Song Name.mp3" -> "Song Name"
    name = path.stem
    return re.sub(r"^\d+\s+-\s+", "", name).strip()


def read_first_cover(mp3_files: list[Path]):
    for file in mp3_files:
        try:
            tags = ID3(file)
            for frame in tags.values():
                if isinstance(frame, APIC):
                    return frame.data, frame.mime
        except Exception:
            pass

    return None, None


def apply_album_metadata(mp3_files: list[Path], album: str, album_artist: str, year: str):
    total_tracks = len(mp3_files)
    cover_data, cover_mime = read_first_cover(mp3_files)

    for file in mp3_files:
        track_no = get_track_number(file)
        title = get_title_from_filename(file)

        try:
            tags = ID3(file)
        except error:
            tags = ID3()

        # Remove old album art so every file gets the same one
        tags.delall("APIC")

        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        tags.setall("TALB", [TALB(encoding=3, text=album)])
        tags.setall("TPE1", [TPE1(encoding=3, text=album_artist)])
        tags.setall("TPE2", [TPE2(encoding=3, text=album_artist)])

        if year:
            tags.setall("TDRC", [TDRC(encoding=3, text=year)])

        if track_no:
            tags.setall("TRCK", [TRCK(encoding=3, text=f"{track_no}/{total_tracks}")])

        if cover_data:
            tags.add(
                APIC(
                    encoding=3,
                    mime=cover_mime or "image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover_data,
                )
            )

        # ID3 v2.3 works better with Apple/iPhone/music players
        tags.save(file, v2_version=3)

        print(f"Tagged: {file.name}")


link = input("Paste playlist / album link: ").strip()
album = input("Album name: ").strip()
album_artist = input("Album artist: ").strip()
year = input("Year: ").strip()

if not link:
    print("No link provided.")
    exit(1)

before = set(SONGS_DIR.glob("*.mp3"))

command = [
    "yt-dlp",

    # Helps avoid YouTube 403 errors
    "--js-runtimes", "node",

    "-x",
    "--audio-format", "mp3",
    "--audio-quality", "0",
    "--embed-thumbnail",
    "--add-metadata",
    "--yes-playlist",

    # Keep files directly inside aria-server/songs
    # Playlist position becomes track number
    "-o", str(SONGS_DIR / "%(playlist_index)02d - %(title)s.%(ext)s"),

    link,
]

try:
    subprocess.run(command, check=True)
except subprocess.CalledProcessError:
    print("Download failed.")
    exit(1)

after = set(SONGS_DIR.glob("*.mp3"))
new_files = sorted(after - before)

if not new_files:
    print("No new MP3 files found.")
    print("Refusing to tag old files in songs folder.")
    exit(1)

if not new_files:
    print("No MP3 files found.")
    exit(1)

apply_album_metadata(new_files, album, album_artist, year)

print(f"\nDone. Files saved to: {SONGS_DIR}")