#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

GENERIC_ARTISTS = {"youtube", "youtube music", "various artists"}


def _first_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _artist_names(info: dict) -> list[str]:
    artists = info.get("artists")
    names: list[str] = []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict):
                name = _first_text(artist.get("name"))
            else:
                name = _first_text(artist)
            if name and name.casefold() not in {item.casefold() for item in names}:
                names.append(name)
    return names


def source_track_metadata(info: dict) -> dict:
    artist_names = _artist_names(info)
    artist = _first_text(info.get("artist"))
    if not artist and artist_names:
        artist = ", ".join(artist_names)
    if not artist:
        artist = _first_text(info.get("uploader") or info.get("channel"))
    artist = re.sub(r"\s+-\s+Topic$", "", artist, flags=re.IGNORECASE).strip()

    raw_title = _first_text(info.get("track") or info.get("alt_title") or info.get("title"))
    if artist.casefold() in GENERIC_ARTISTS or not artist:
        parts = re.split(r"\s+[-–—]\s+", raw_title, maxsplit=1)
        if len(parts) == 2:
            artist = parts[0].strip()
            raw_title = parts[1].strip()
    elif raw_title.casefold().startswith(f"{artist.casefold()} - "):
        raw_title = raw_title[len(artist) + 3 :].strip()

    album = _first_text(info.get("album"))
    album_artist = _first_text(info.get("album_artist")) or artist
    year = _first_text(
        info.get("release_year")
        or info.get("release_date")
        or info.get("upload_date")
    )
    if len(year) >= 4 and year[:4].isdigit():
        year = year[:4]

    track_number = _first_text(info.get("track_number") or info.get("track"))
    if not track_number.isdigit():
        track_number = ""

    genres = info.get("genres")
    genre = ", ".join(str(item).strip() for item in genres if str(item).strip()) if isinstance(genres, list) else ""

    return {
        "title": raw_title,
        "artist": artist,
        "album": album,
        "albumArtist": album_artist,
        "year": year,
        "trackNumber": track_number,
        "genre": genre,
    }


def apply_youtube_metadata(
    mp3_path: Path,
    info: dict,
    *,
    artwork_path: Path | None = None,
) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, error

    metadata = source_track_metadata(info)
    try:
        tags = ID3(mp3_path)
    except error:
        tags = ID3()

    def replace_text(frame_id: str, frame_type, value: str) -> None:
        tags.delall(frame_id)
        if value:
            tags.add(frame_type(encoding=3, text=value))

    replace_text("TIT2", TIT2, metadata["title"])
    replace_text("TPE1", TPE1, metadata["artist"])
    replace_text("TPE2", TPE2, metadata["albumArtist"])
    replace_text("TALB", TALB, metadata["album"])
    replace_text("TDRC", TDRC, metadata["year"])
    replace_text("TRCK", TRCK, metadata["trackNumber"])
    replace_text("TCON", TCON, metadata["genre"])

    if artwork_path is not None and artwork_path.is_file():
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=artwork_path.read_bytes(),
            )
        )

    tags.save(mp3_path, v2_version=3)


def apply_downloaded_info_sidecars(mp3_files: list[Path]) -> int:
    updated = 0
    for mp3_path in mp3_files:
        sidecar = mp3_path.with_suffix(".info.json")
        if not sidecar.is_file():
            raise RuntimeError(f"YouTube metadata is missing for {mp3_path.name}")
        try:
            info = json.loads(sidecar.read_text(encoding="utf-8"))
            apply_youtube_metadata(mp3_path, info)
            updated += 1
        finally:
            sidecar.unlink(missing_ok=True)
    return updated


def refresh_tracks_from_youtube(
    items: list[dict],
    *,
    downloader: str,
    base_dir: Path,
) -> int:
    valid_items = [
        item
        for item in items
        if item.get("id") and Path(item.get("path", "")).is_file()
    ]
    if not valid_items:
        return 0

    with tempfile.TemporaryDirectory(prefix="aria-track-metadata-") as directory:
        output_dir = Path(directory)
        command = [
            downloader,
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "youtube:player_client=web_embedded",
            "--skip-download",
            "--write-info-json",
            "--clean-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--no-playlist",
            "--no-warnings",
            "-o",
            str(output_dir / "%(id)s.%(ext)s"),
            *[f"https://music.youtube.com/watch?v={item['id']}" for item in valid_items],
        ]
        result = subprocess.run(
            command,
            cwd=str(base_dir),
            capture_output=True,
            text=True,
            timeout=max(90, len(valid_items) * 30),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "Could not refresh YouTube metadata")

        updated = 0
        for item in valid_items:
            video_id = str(item["id"])
            info_path = output_dir / f"{video_id}.info.json"
            artwork_path = output_dir / f"{video_id}.jpg"
            if not info_path.is_file():
                raise RuntimeError(f"YouTube metadata is missing for {video_id}")
            info = json.loads(info_path.read_text(encoding="utf-8"))
            apply_youtube_metadata(
                Path(item["path"]),
                info,
                artwork_path=artwork_path if artwork_path.is_file() else None,
            )
            updated += 1
        return updated
