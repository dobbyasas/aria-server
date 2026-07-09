#!/usr/bin/env python3
from pathlib import Path
from mutagen import File
1
BASE_DIR = Path(__file__).resolve().parent.parent
SONGS_DIR = BASE_DIR / "songs"

TARGET_ALBUM = "you are we"


def get_first_tag(audio, key: str) -> str:
    if not audio or not audio.tags:
        return ""

    value = audio.tags.get(key)

    if not value:
        return ""

    if isinstance(value, list):
        return str(value[0]).strip()

    return str(value).strip()


def normalize(text: str) -> str:
    return text.lower().strip()


matches = []

for file in sorted(SONGS_DIR.glob("*.mp3")):
    try:
        audio = File(file, easy=True)

        if not audio or not audio.tags:
            continue

        album = normalize(get_first_tag(audio, "album"))

        if album == TARGET_ALBUM:
            matches.append(file)

    except Exception as e:
        print(f"Skipped {file.name}: {e}")


if not matches:
    print('No songs found from album "You Are We".')
    exit(0)


print('\nThese songs are from album "You Are We" and will be deleted:\n')

for i, file in enumerate(matches, start=1):
    audio = File(file, easy=True)

    title = get_first_tag(audio, "title") or "NO TITLE"
    artist = get_first_tag(audio, "artist") or "NO ARTIST"
    album_artist = get_first_tag(audio, "albumartist") or "NO ALBUM ARTIST"
    album = get_first_tag(audio, "album") or "NO ALBUM"

    print(f"{i}. {file.name}")
    print(f"   Title:        {title}")
    print(f"   Artist:       {artist}")
    print(f"   Album artist: {album_artist}")
    print(f"   Album:        {album}")
    print()


print(f"Total: {len(matches)} file(s)")

confirm = input('Delete the whole "You Are We" album? Type DELETE to confirm: ').strip()

if confirm != "DELETE":
    print("Cancelled. Nothing deleted.")
    exit(0)


for file in matches:
    file.unlink()
    print(f"Deleted: {file.name}")


print("\nDone.")