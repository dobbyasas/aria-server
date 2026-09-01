#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

SCRIPTS_MODULE_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_MODULE_DIR))

from youtube_track_metadata import refresh_tracks_from_youtube


SONG_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac"}
ARIA_VERSION = "1.14.2"
CATALOG_INDEX_VERSION = 4
CATALOG_REFRESH_INTERVAL_SECONDS = 10
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
MAX_DOWNLOAD_HISTORY = 8
PALETTES = [
    ("#45D6C7", "#26324A", "waveform"),
    ("#F28482", "#2E1E32", "sparkles"),
    ("#A7C957", "#1A2C2A", "moon.stars.fill"),
    ("#F4D35E", "#23395B", "tram.fill"),
    ("#8ECAE6", "#1D3557", "drop.fill"),
    ("#B8F2E6", "#21455A", "clock.fill"),
    ("#C77DFF", "#2B235A", "antenna.radiowaves.left.and.right"),
    ("#90BE6D", "#22332C", "airplane"),
]

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")
LRC_LINE_RE = re.compile(r"^\s*((?:\[\d{1,3}:\d{2}(?:\.\d{1,3})?\])+)(.*)$")
LRC_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")
LYRICS_CACHE_VERSION = 1
LYRICS_NOT_FOUND_TTL_SECONDS = 24 * 60 * 60
LRCLIB_BASE_URL = "https://lrclib.net/api/get"
PLAYLIST_STORE_VERSION = 1
MAX_PLAYLIST_COVER_BYTES = 2 * 1024 * 1024
MAX_ARTWORK_BYTES = 12 * 1024 * 1024
STANDALONE_TRACK_STORE_VERSION = 1
STANDALONE_TRACK_STORE_NAME = ".aria_standalone_tracks.json"
YOUTUBE_ARTWORK_HOST_SUFFIXES = ("googleusercontent.com", "ytimg.com")
ARTWORK_REQUEST_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


def downloader_executable() -> str:
    configured = os.environ.get("ARIA_YT_DLP", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(
        Path.home() / ".local" / "share" / "aria-downloader" / "current" / "bin" / "yt-dlp"
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("yt-dlp") or "yt-dlp"


def normalized_download_identity(value: str | None) -> str:
    value = str(value or "")
    value = re.sub(r"\([^)]*\)|\[[^]]*]", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def normalized_download_artist(value: str | None) -> str:
    words = normalized_download_identity(value).split()
    ignored = {"official", "topic", "vevo", "music"}
    normalized_words: list[str] = []
    for word in words:
        if word in ignored:
            continue
        for suffix in ("official", "topic", "vevo"):
            if word.endswith(suffix) and len(word) > len(suffix):
                word = word.removesuffix(suffix)
                break
        if word:
            normalized_words.append(word)
    return " ".join(normalized_words)


def download_title_identities(value: str | None) -> set[str]:
    raw_value = str(value or "").strip()
    candidates = {normalized_download_identity(raw_value)}
    parts = re.split(r"\s+[-–—]\s+", raw_value, maxsplit=1)
    if len(parts) == 2:
        candidates.add(normalized_download_identity(parts[1]))
    return {candidate for candidate in candidates if candidate}


def download_artists_match(first: str | None, second: str | None) -> bool:
    left = normalized_download_artist(first)
    right = normalized_download_artist(second)
    if not left or not right:
        return False
    return left == right or left.replace(" ", "") == right.replace(" ", "")


def youtube_id_from_filename(filename: str) -> str | None:
    match = re.search(r"\[([A-Za-z0-9_-]{6,})]\.[^.]+$", filename)
    return match.group(1) if match else None


def default_songs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "songs"


def song_files(songs_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in songs_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SONG_EXTENSIONS
        ),
        key=lambda path: path.name.lower(),
    )


def standalone_track_filenames(songs_dir: Path) -> set[str]:
    path = songs_dir / STANDALONE_TRACK_STORE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    if payload.get("version") != STANDALONE_TRACK_STORE_VERSION:
        return set()

    filenames = payload.get("filenames", [])
    if not isinstance(filenames, list):
        return set()
    return {
        str(filename)
        for filename in filenames
        if isinstance(filename, str) and filename
    }


def save_standalone_track_filenames(songs_dir: Path, filenames: set[str]) -> None:
    path = songs_dir / STANDALONE_TRACK_STORE_NAME
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "version": STANDALONE_TRACK_STORE_VERSION,
                "filenames": sorted(filenames, key=str.casefold),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def ffprobe_metadata(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:format_tags=title,artist,album,album_artist,albumartist,date,track,tracknumber:stream=codec_type:stream_disposition=attached_pic",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return {}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    file_format = payload.get("format", {})
    tags = {key.lower(): value for key, value in file_format.get("tags", {}).items()}
    streams = payload.get("streams", [])

    return {
        "duration": float(file_format.get("duration", 0) or 0),
        "title": tags.get("title"),
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "albumArtist": album_artist_from_tags(tags),
        "date": tags.get("date"),
        "trackNumber": track_number_from_tag(tags.get("track") or tags.get("tracknumber")),
        "hasArtwork": any(
            stream.get("codec_type") == "video"
            and stream.get("disposition", {}).get("attached_pic") == 1
            for stream in streams
        ),
    }


def title_from_filename(path: Path) -> tuple[str, str]:
    stem = path.stem.replace("_", " ")
    stem = re.sub(r"\s+\d+$", "", stem).strip()

    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip() or stem, artist.strip() or "Unknown Artist"

    return stem or path.stem, "Unknown Artist"


def year_from_date(value: str | None) -> int:
    if value:
        match = re.search(r"\d{4}", value)
        if match:
            return int(match.group(0))

    return datetime.now().year


def track_number_from_tag(value: str | None) -> int | None:
    if not value:
        return None

    match = re.search(r"\d+", str(value))
    if not match:
        return None

    return int(match.group(0))


def album_artist_from_tags(tags: dict) -> str | None:
    for key in ("album_artist", "albumartist", "album artist", "album-artist"):
        value = tags.get(key)
        if value:
            return str(value)

    return None


def palette_for(path: Path) -> dict:
    palette_index = uuid.uuid5(uuid.NAMESPACE_URL, path.name).int % len(PALETTES)
    top_hex, bottom_hex, symbol_name = PALETTES[palette_index]
    return {
        "topHex": top_hex,
        "bottomHex": bottom_hex,
        "symbolName": symbol_name,
    }


def normalized_album_title(album: str | None) -> str:
    title = str(album or "Fedora songs").strip()
    return title or "Fedora songs"


def album_key(_path: Path, metadata: dict) -> str:
    return normalized_album_title(metadata.get("album")).casefold()


def track_payload(path: Path, base_url: str, metadata: dict, artwork_url: str | None) -> dict:
    fallback_title, fallback_artist = title_from_filename(path)
    filename = quote(path.name)

    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path.name)),
        "albumID": album_id_for(metadata.get("album") or "Fedora songs"),
        "title": metadata.get("title") or fallback_title,
        "artist": metadata.get("artist") or fallback_artist,
        "albumArtist": metadata.get("albumArtist") or metadata.get("artist") or fallback_artist,
        "album": metadata.get("album") or "Fedora songs",
        "duration": metadata.get("duration") or 0,
        "year": year_from_date(metadata.get("date")),
        "trackNumber": metadata.get("trackNumber"),
        "artwork": palette_for(path),
        "streamURL": f"{base_url}/api/stream/{filename}",
        "artworkURL": artwork_url,
        "isExplicit": False,
    }


def build_track_record(path: Path, metadata: dict, is_standalone: bool = False) -> dict:
    fallback_title, fallback_artist = title_from_filename(path)
    stat = path.stat()

    title = metadata.get("title") or fallback_title
    artist = metadata.get("artist") or fallback_artist
    album_artist = metadata.get("albumArtist") or artist
    album = metadata.get("album") or "Fedora songs"

    record = {
        "filename": path.name,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path.name)),
        "title": title,
        "artist": artist,
        "albumArtist": album_artist,
        "album": album,
        "duration": metadata.get("duration") or 0,
        "year": year_from_date(metadata.get("date")),
        "trackNumber": metadata.get("trackNumber"),
        "artwork": palette_for(path),
        "hasArtwork": bool(metadata.get("hasArtwork")),
        "isExplicit": False,
        "isStandalone": is_standalone,
    }
    record["searchText"] = search_text_for(record)
    record["albumID"] = album_id_for(album)
    return record


def search_text_for(record: dict) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "artist", "albumArtist", "album", "filename")
    ).casefold()


def album_id_for(album: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aria-album:{normalized_album_title(album).casefold()}"))


def album_key_for_record(record: dict) -> str:
    return normalized_album_title(record.get("album")).casefold()


def artwork_url_for_record(record: dict | None, base_url: str) -> str | None:
    if not record:
        return None

    filename = quote(str(record["filename"]))
    version = quote(str(record.get("mtimeNs") or "0"))
    return f"{base_url}/api/artwork/{filename}?v={version}"


def preferred_artist(values: list[str | None]) -> str | None:
    counts: dict[str, int] = {}
    first_indexes: dict[str, int] = {}
    display_values: dict[str, str] = {}

    for index, value in enumerate(values):
        artist = str(value or "").strip()
        if not artist:
            continue

        key = artist.casefold()
        counts[key] = counts.get(key, 0) + 1
        first_indexes.setdefault(key, index)
        display_values.setdefault(key, artist)

    if not counts:
        return None

    best_key = max(counts, key=lambda key: (counts[key], -first_indexes[key]))
    return display_values[best_key]


def album_artist_for_records(records: list[dict]) -> str:
    return (
        preferred_artist([record.get("albumArtist") for record in records])
        or preferred_artist([record.get("artist") for record in records])
        or "Unknown Artist"
    )


def track_sort_key(record: dict) -> tuple:
    return (
        str(record.get("album") or "").casefold(),
        record.get("trackNumber") if record.get("trackNumber") is not None else 999_999,
        str(record.get("artist") or "").casefold(),
        str(record.get("title") or "").casefold(),
        str(record.get("filename") or "").casefold(),
    )


def title_sort_key(record: dict) -> tuple:
    return (
        str(record.get("title") or "").casefold(),
        str(record.get("artist") or "").casefold(),
        str(record.get("album") or "").casefold(),
        str(record.get("filename") or "").casefold(),
    )


def matches_query(record: dict, query: str) -> bool:
    if not query:
        return True

    search_text = str(record.get("searchText") or "").casefold()
    return all(token in search_text for token in query.casefold().split())


def track_payload_from_record(record: dict, base_url: str, artwork_source_record: dict | None = None) -> dict:
    filename = quote(str(record["filename"]))
    artwork_url = artwork_url_for_record(artwork_source_record, base_url)

    return {
        "id": record["id"],
        "albumID": record.get("albumID") or album_id_for(record.get("album") or "Fedora songs"),
        "title": record.get("title") or Path(record["filename"]).stem,
        "artist": record.get("artist") or "Unknown Artist",
        "albumArtist": record.get("albumArtist") or record.get("artist") or "Unknown Artist",
        "album": record.get("album") or "Fedora songs",
        "duration": record.get("duration") or 0,
        "year": record.get("year") or datetime.now().year,
        "trackNumber": record.get("trackNumber"),
        "artwork": record.get("artwork") or palette_for(Path(record["filename"])),
        "streamURL": f"{base_url}/api/stream/{filename}",
        "artworkURL": artwork_url,
        "isExplicit": bool(record.get("isExplicit")),
        "isStandalone": bool(record.get("isStandalone")),
    }


def album_artwork_sources(records: list[dict]) -> dict[str, dict]:
    artwork_by_album: dict[str, dict] = {}

    for record in records:
        if not record.get("hasArtwork"):
            continue

        key = album_key_for_record(record)
        if key not in artwork_by_album:
            artwork_by_album[key] = record

    return artwork_by_album


def paged_payload(items: list, offset: int, limit: int) -> dict:
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "items": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(page) < total,
    }


def query_text(params: dict[str, list[str]]) -> str:
    return first_query_value(params, "q", "query", "search").strip()


def first_query_value(params: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = params.get(name)
        if values:
            return values[0]

    return ""


def query_int(params: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(first_query_value(params, name) or default)
    except ValueError:
        value = default

    return min(max(value, minimum), maximum)


class CatalogIndex:
    def __init__(self, songs_dir: Path, index_path: Path | None = None) -> None:
        self.songs_dir = songs_dir
        self.index_path = index_path or songs_dir / ".aria_catalog_index.json"
        self.lock = threading.RLock()
        self.records: list[dict] = []
        self.records_by_filename: dict[str, dict] = {}
        self.last_refresh_at = 0.0
        self.last_refresh_started_at = 0.0
        self.is_refreshing = False
        self.last_error: str | None = None
        self.load_cached_records()

    def tracks(self) -> list[dict]:
        self.refresh_in_background()
        with self.lock:
            return list(self.records)

    def track_for_filename(self, filename: str) -> dict | None:
        self.refresh_in_background()
        with self.lock:
            record = self.records_by_filename.get(filename)
            return dict(record) if record else None

    def track_for_id(self, track_id: str) -> dict | None:
        self.refresh_in_background()
        with self.lock:
            for record in self.records:
                if record.get("id") == track_id:
                    return dict(record)
        return None

    def update_track_metadata(self, track_id: str, updates: dict) -> dict | None:
        with self.lock:
            record_index = next(
                (
                    index
                    for index, record in enumerate(self.records)
                    if record.get("id") == track_id
                ),
                None,
            )
            if record_index is None:
                return None

            updated_record = dict(self.records[record_index])
            updated_record.update(updates)
            updated_record["searchText"] = search_text_for(updated_record)
            updated_record["albumID"] = album_id_for(updated_record.get("album") or "Fedora songs")

            records = list(self.records)
            records[record_index] = updated_record
            records.sort(key=title_sort_key)

            self.records = records
            self.records_by_filename = {record["filename"]: record for record in records}
            self.last_refresh_at = monotonic()
            self.last_error = None

            self.save(records)
            return dict(updated_record)

    def refresh_album_artwork(
        self,
        track_id: str,
        artwork_data: bytes,
        artwork_mime: str,
    ) -> tuple[dict, int] | None:
        with self.lock:
            target_record = next(
                (record for record in self.records if record.get("id") == track_id),
                None,
            )
            if target_record is None:
                return None

            target_album_key = album_key_for_record(target_record)
            album_records = [
                record
                for record in self.records
                if album_key_for_record(record) == target_album_key
            ]
            refreshed_filenames: set[str] = set()

            try:
                for record in album_records:
                    path = self.songs_dir / str(record["filename"])
                    if not path.is_file():
                        raise ArtworkRefreshError(f"Song file is missing: {path.name}")
                    replace_embedded_artwork(path, artwork_data, artwork_mime)
                    refreshed_filenames.add(path.name)
            finally:
                if refreshed_filenames:
                    refreshed_records: list[dict] = []
                    for record in self.records:
                        refreshed_record = dict(record)
                        if record.get("filename") in refreshed_filenames:
                            path = self.songs_dir / str(record["filename"])
                            stat = path.stat()
                            refreshed_record["size"] = stat.st_size
                            refreshed_record["mtimeNs"] = stat.st_mtime_ns
                            refreshed_record["hasArtwork"] = True
                        refreshed_records.append(refreshed_record)

                    refreshed_records.sort(key=title_sort_key)
                    self.records = refreshed_records
                    self.records_by_filename = {
                        record["filename"]: record for record in refreshed_records
                    }
                    self.last_refresh_at = monotonic()
                    self.last_error = None
                    self.save(refreshed_records)

            updated_target = next(
                record for record in self.records if record.get("id") == track_id
            )
            return dict(updated_target), len(album_records)

    def status(self) -> dict:
        with self.lock:
            return {
                "isIndexing": self.is_refreshing,
                "lastRefreshStartedAt": self.last_refresh_started_at,
                "lastRefreshFinishedAt": self.last_refresh_at,
                "lastError": self.last_error,
            }

    def load_cached_records(self) -> None:
        cache = self.load()
        records = [
            self.normalized_record(record)
            for record in cache.get("tracks", {}).values()
            if isinstance(record, dict) and record.get("filename")
        ]
        records.sort(key=title_sort_key)

        with self.lock:
            self.records = records
            self.records_by_filename = {record["filename"]: record for record in records}

    def normalized_record(self, record: dict) -> dict:
        record = dict(record)
        record.setdefault("searchText", search_text_for(record))
        record["albumID"] = album_id_for(record.get("album") or "Fedora songs")
        record.setdefault("albumArtist", record.get("artist") or "Unknown Artist")
        record.setdefault("hasArtwork", False)
        record.setdefault("isExplicit", False)
        record.setdefault("isStandalone", False)
        record.setdefault("artwork", palette_for(Path(record["filename"])))
        return record

    def refresh_in_background(self, force: bool = False) -> None:
        with self.lock:
            now = monotonic()
            if self.is_refreshing:
                return
            if (
                not force
                and self.last_refresh_at
                and now - self.last_refresh_at < CATALOG_REFRESH_INTERVAL_SECONDS
            ):
                return

            self.is_refreshing = True
            self.last_refresh_started_at = now

        thread = threading.Thread(
            target=self.refresh_worker,
            args=(force,),
            name="AriaCatalogRefresh",
            daemon=True,
        )
        thread.start()

    def refresh_worker(self, force: bool) -> None:
        try:
            self.refresh(force=force)
        except Exception as error:
            with self.lock:
                self.last_error = str(error)
        finally:
            with self.lock:
                self.is_refreshing = False

    def refresh(self, force: bool = False) -> None:
        now = monotonic()
        with self.lock:
            if (
                not force
                and self.last_refresh_at
                and now - self.last_refresh_at < CATALOG_REFRESH_INTERVAL_SECONDS
            ):
                return

        cache = self.load()
        cached_tracks = cache.get("tracks", {})
        standalone_filenames = standalone_track_filenames(self.songs_dir)
        records: list[dict] = []
        changed = False

        for path in song_files(self.songs_dir):
            stat = path.stat()
            cached_record = cached_tracks.get(path.name)

            if (
                cached_record
                and cached_record.get("size") == stat.st_size
                and cached_record.get("mtimeNs") == stat.st_mtime_ns
            ):
                record = self.normalized_record(cached_record)
            else:
                metadata = ffprobe_metadata(path)
                record = build_track_record(
                    path,
                    metadata,
                    is_standalone=path.name in standalone_filenames,
                )
                changed = True

            is_standalone = path.name in standalone_filenames
            if bool(record.get("isStandalone")) != is_standalone:
                record["isStandalone"] = is_standalone
                changed = True

            records.append(record)

        current_names = {record["filename"] for record in records}
        if current_names != set(cached_tracks.keys()):
            changed = True

        records.sort(key=title_sort_key)

        with self.lock:
            self.records = records
            self.records_by_filename = {record["filename"]: record for record in records}
            self.last_refresh_at = monotonic()
            self.last_error = None

        if changed or cache.get("version") != CATALOG_INDEX_VERSION:
            self.save(records)

    def delete_album(self, album_id: str) -> tuple[int, set[str]] | None:
        with self.lock:
            records = [
                dict(record)
                for record in self.records
                if record.get("albumID") == album_id
                and not bool(record.get("isStandalone"))
            ]

        if not records:
            return None

        deleted_track_ids: set[str] = set()
        deleted_filenames: set[str] = set()
        songs_root = self.songs_dir.resolve()

        for record in records:
            filename = str(record.get("filename") or "")
            path = (self.songs_dir / filename).resolve()
            if not filename or path.parent != songs_root:
                raise OSError("Album contains an unsafe filename")
            if path.is_file():
                path.unlink()
            for suffix in (".lrc", ".lyrics", ".txt"):
                sidecar = path.with_suffix(suffix)
                if sidecar.is_file():
                    sidecar.unlink()
            deleted_track_ids.add(str(record.get("id")))
            deleted_filenames.add(filename)

        standalone = standalone_track_filenames(self.songs_dir)
        if standalone.intersection(deleted_filenames):
            save_standalone_track_filenames(
                self.songs_dir,
                standalone.difference(deleted_filenames),
            )

        self.refresh(force=True)
        return len(deleted_filenames), deleted_track_ids

    def delete_track_records(self, records: list[dict]) -> set[str]:
        deleted_track_ids: set[str] = set()
        deleted_filenames: set[str] = set()
        songs_root = self.songs_dir.resolve()

        for record in records:
            filename = str(record.get("filename") or "")
            path = (self.songs_dir / filename).resolve()
            if not filename or path.parent != songs_root:
                raise OSError("Track contains an unsafe filename")
            if path.is_file():
                path.unlink()
            for suffix in (".lrc", ".lyrics", ".txt"):
                sidecar = path.with_suffix(suffix)
                if sidecar.is_file():
                    sidecar.unlink()
            deleted_track_ids.add(str(record.get("id")))
            deleted_filenames.add(filename)

        standalone = standalone_track_filenames(self.songs_dir)
        if standalone.intersection(deleted_filenames):
            save_standalone_track_filenames(
                self.songs_dir,
                standalone.difference(deleted_filenames),
            )

        if deleted_filenames:
            self.refresh(force=True)
        return deleted_track_ids

    def load(self) -> dict:
        try:
            with self.index_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": CATALOG_INDEX_VERSION, "tracks": {}}

        if payload.get("version") != CATALOG_INDEX_VERSION:
            return {"version": CATALOG_INDEX_VERSION, "tracks": {}}

        tracks = payload.get("tracks", {})
        if not isinstance(tracks, dict):
            tracks = {}

        return {"version": CATALOG_INDEX_VERSION, "tracks": tracks}

    def save(self, records: list[dict]) -> None:
        payload = {
            "version": CATALOG_INDEX_VERSION,
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "songsDir": str(self.songs_dir),
            "tracks": {record["filename"]: record for record in records},
        }
        temporary_path = self.index_path.with_suffix(".tmp")

        try:
            with temporary_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
            temporary_path.replace(self.index_path)
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def parse_synced_lyrics(text: str | None) -> list[dict]:
    if not text:
        return []

    timed_lines: list[tuple[float, str]] = []
    for raw_line in text.splitlines():
        match = LRC_LINE_RE.match(raw_line)
        if not match:
            continue

        timestamps, lyric_text = match.groups()
        lyric_text = lyric_text.strip()
        for timestamp in LRC_TIMESTAMP_RE.finditer(timestamps):
            minutes, seconds, fraction = timestamp.groups()
            fraction_value = float(f"0.{fraction}") if fraction else 0.0
            start_time = int(minutes) * 60 + int(seconds) + fraction_value
            timed_lines.append((start_time, lyric_text))

    timed_lines.sort(key=lambda item: item[0])
    return [
        {
            "id": f"{index}-{start_time:.3f}",
            "startTime": round(start_time, 3),
            "text": lyric_text,
        }
        for index, (start_time, lyric_text) in enumerate(timed_lines)
    ]


def plain_lyrics_from_synced_lines(lines: list[dict]) -> str | None:
    text = "\n".join(line["text"] for line in lines if line.get("text")).strip()
    return text or None


def local_lyrics_text(path: Path) -> tuple[str, str] | None:
    for suffix in (".lrc", ".lyrics", ".txt"):
        sidecar = path.with_suffix(suffix)
        if not sidecar.exists() or not sidecar.is_file():
            continue

        try:
            text = sidecar.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            continue

        if text:
            return text, "sidecar"

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return None

    tags = {
        str(key).casefold().replace("_", "").replace(" ", ""): value
        for key, value in payload.get("format", {}).get("tags", {}).items()
    }
    for key in ("syncedlyrics", "unsyncedlyrics", "lyrics", "lyric"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), "embedded"

    return None


def lyrics_result(
    track_id: str,
    source: str,
    plain_lyrics: str | None = None,
    synced_lyrics: str | None = None,
    instrumental: bool = False,
) -> dict:
    synced_lines = parse_synced_lyrics(synced_lyrics)
    clean_plain_lyrics = str(plain_lyrics or "").strip() or plain_lyrics_from_synced_lines(synced_lines)
    available = instrumental or bool(clean_plain_lyrics) or bool(synced_lines)

    return {
        "trackID": track_id,
        "available": available,
        "instrumental": instrumental,
        "isSynced": bool(synced_lines),
        "source": source if available else "none",
        "plainLyrics": clean_plain_lyrics,
        "syncedLines": synced_lines,
    }


class LyricsManager:
    def __init__(self, songs_dir: Path, cache_dir: Path | None = None) -> None:
        self.songs_dir = songs_dir.resolve()
        self.cache_dir = (
            cache_dir
            or Path(os.environ.get(
                "ARIA_LYRICS_CACHE_DIR",
                str(Path.home() / ".cache" / "aria-song-server" / "lyrics"),
            ))
        ).expanduser().resolve()
        self.lock = threading.RLock()

    def lyrics_for(self, record: dict) -> dict:
        track_id = str(record.get("id") or "")
        path = (self.songs_dir / str(record.get("filename") or "")).resolve()
        if not track_id or path.parent != self.songs_dir or not path.is_file():
            return lyrics_result(track_id, "none")

        local_lyrics = local_lyrics_text(path)
        if local_lyrics:
            text, source = local_lyrics
            lines = parse_synced_lyrics(text)
            return lyrics_result(
                track_id,
                source,
                plain_lyrics=None if lines else text,
                synced_lyrics=text if lines else None,
            )

        signature = self.signature_for(record)
        cached = self.load_cached(track_id, signature)
        if cached is not None:
            return cached

        result = self.fetch_from_lrclib(record)
        self.save_cached(track_id, signature, result)
        return result

    def fetch_from_lrclib(self, record: dict) -> dict:
        track_id = str(record.get("id") or "")
        title = str(record.get("title") or "").strip()
        artist = str(record.get("artist") or "").strip()
        if not title or not artist or artist.casefold() == "unknown artist":
            return lyrics_result(track_id, "none")

        params = {
            "track_name": title,
            "artist_name": artist,
        }
        album = str(record.get("album") or "").strip()
        if album and album.casefold() not in {"unknown album", "fedora songs"}:
            params["album_name"] = album

        duration = round(float(record.get("duration") or 0))
        if 1 <= duration <= 3600:
            params["duration"] = str(duration)

        request = Request(
            f"{LRCLIB_BASE_URL}?{urlencode(params)}",
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    f"AriaSongServer/{ARIA_VERSION} "
                    "(https://github.com/dobbyasas/aria-server)"
                ),
            },
        )

        try:
            with urlopen(request, timeout=8) as response:
                payload = json.load(response)
        except HTTPError:
            return lyrics_result(track_id, "none")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return lyrics_result(track_id, "none")

        if not isinstance(payload, dict):
            return lyrics_result(track_id, "none")

        return lyrics_result(
            track_id,
            "lrclib",
            plain_lyrics=payload.get("plainLyrics"),
            synced_lyrics=payload.get("syncedLyrics"),
            instrumental=bool(payload.get("instrumental")),
        )

    def signature_for(self, record: dict) -> str:
        return "|".join(
            str(record.get(key) or "")
            for key in ("filename", "mtimeNs", "title", "artist", "album", "duration")
        )

    def cache_path(self, track_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", track_id)
        return self.cache_dir / f"{safe_id}.json"

    def load_cached(self, track_id: str, signature: str) -> dict | None:
        path = self.cache_path(track_id)
        with self.lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return None

        if payload.get("version") != LYRICS_CACHE_VERSION or payload.get("signature") != signature:
            return None

        result = payload.get("result")
        if not isinstance(result, dict):
            return None

        expires_at = payload.get("expiresAt")
        if expires_at is not None:
            try:
                if float(expires_at or 0) <= time.time():
                    return None
            except (TypeError, ValueError):
                return None

        return result

    def save_cached(self, track_id: str, signature: str, result: dict) -> None:
        payload = {
            "version": LYRICS_CACHE_VERSION,
            "signature": signature,
            "result": result,
        }
        if not result.get("available"):
            payload["expiresAt"] = time.time() + LYRICS_NOT_FOUND_TTL_SECONDS

        temporary_path = self.cache_path(track_id).with_suffix(".tmp")
        with self.lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary_path.replace(self.cache_path(track_id))
            except OSError:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def embedded_artwork(path: Path) -> bytes | None:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    return result.stdout or None


class ArtworkRefreshError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_GATEWAY) -> None:
        super().__init__(message)
        self.status = status


def is_youtube_artwork_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in YOUTUBE_ARTWORK_HOST_SUFFIXES
    )


def detected_artwork_mime(data: bytes, advertised_mime: str | None = None) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"

    advertised = str(advertised_mime or "").split(";", 1)[0].strip().casefold()
    if advertised in {"image/jpeg", "image/png", "image/webp"} and data:
        return advertised
    return None


def download_youtube_artwork(source_url: str) -> tuple[bytes, str]:
    if not is_youtube_artwork_url(source_url):
        raise ArtworkRefreshError(
            "Artwork URL must be an HTTPS image hosted by YouTube.",
            HTTPStatus.BAD_REQUEST,
        )

    request = Request(
        source_url,
        headers={
            "Accept": "image/jpeg,image/png,*/*;q=0.8",
            "User-Agent": ARTWORK_REQUEST_USER_AGENT,
        },
    )

    try:
        with urlopen(request, timeout=20) as response:
            advertised_mime = response.headers.get("Content-Type")
            artwork_data = response.read(MAX_ARTWORK_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ArtworkRefreshError(f"YouTube artwork download failed: {error}") from error

    if len(artwork_data) > MAX_ARTWORK_BYTES:
        raise ArtworkRefreshError(
            "YouTube artwork is too large.",
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )

    artwork_mime = detected_artwork_mime(artwork_data, advertised_mime)
    if artwork_mime is None:
        raise ArtworkRefreshError("YouTube did not return a supported image.")

    return artwork_data, artwork_mime


def replace_embedded_artwork(path: Path, artwork_data: bytes, artwork_mime: str) -> None:
    try:
        if path.suffix.casefold() in {".mp3", ".aac", ".wav"}:
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError

            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=artwork_mime,
                    type=3,
                    desc="Cover",
                    data=artwork_data,
                )
            )
            tags.save(path, v2_version=3)
            return

        if path.suffix.casefold() == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(path)
            image_format = (
                MP4Cover.FORMAT_PNG
                if artwork_mime == "image/png"
                else MP4Cover.FORMAT_JPEG
            )
            audio["covr"] = [MP4Cover(artwork_data, imageformat=image_format)]
            audio.save()
            return

        if path.suffix.casefold() == ".flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(path)
            audio.clear_pictures()
            picture = Picture()
            picture.type = 3
            picture.mime = artwork_mime
            picture.desc = "Cover"
            picture.data = artwork_data
            audio.add_picture(picture)
            audio.save()
            return
    except (OSError, ValueError) as error:
        raise ArtworkRefreshError(f"Could not replace artwork in {path.name}: {error}") from error

    raise ArtworkRefreshError(
        f"Artwork replacement is not supported for {path.suffix or path.name}.",
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    )


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def plain_output_line(line: str) -> str:
    return ANSI_ESCAPE_RE.sub("", line).replace("\r", "").strip()


class DownloadBusyError(Exception):
    pass


class DownloadValidationError(ValueError):
    pass


class DownloadJob:
    def __init__(self, link: str, album: str, album_artist: str, year: str, kind: str) -> None:
        self.id = str(uuid.uuid4())
        self.link = link
        self.album = album
        self.album_artist = album_artist
        self.year = year
        self.kind = kind
        self.status = "queued"
        self.phase = "Queued"
        self.message = "Waiting to start"
        self.progress = 0.0
        self.files_started = 0
        self.audio_converted = 0
        self.metadata_lines = 0
        self.cover_lines = 0
        self.new_files: int | None = None
        self.reused_files = 0
        self.playlist_id: str | None = None
        self.playlist_track_count: int | None = None
        self.error: str | None = None
        self.output_tail: list[str] = []
        self.created_at = timestamp()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.lock = threading.RLock()

    @property
    def is_active(self) -> bool:
        return self.status in {"queued", "running"}

    def mark_running(self) -> None:
        with self.lock:
            self.status = "running"
            self.phase = "Starting downloader"
            self.message = "Starting yt-dlp"
            self.progress = max(self.progress, 0.04)
            self.started_at = timestamp()

    def append_output(self, line: str) -> None:
        clean_line = plain_output_line(line)
        if not clean_line:
            return

        with self.lock:
            self.output_tail.append(clean_line)
            self.output_tail = self.output_tail[-30:]
            self.message = clean_line
            self.update_from_output(clean_line)

    def update_from_output(self, line: str) -> None:
        if "[youtube]" in line or "[generic]" in line:
            self.phase = "Reading YouTube data"
            self.progress = max(self.progress, 0.10)

        if "[download] Destination:" in line:
            self.files_started += 1
            self.phase = "Starting file"
            self.progress = max(self.progress, 0.18)
        elif "[download]" in line:
            self.phase = "Downloading"
            match = DOWNLOAD_PROGRESS_RE.search(line)
            if match:
                percent = min(max(float(match.group(1)), 0), 100)
                self.progress = max(self.progress, 0.20 + (percent / 100) * 0.46)
            else:
                self.progress = max(self.progress, 0.24)
        elif "[ExtractAudio]" in line:
            self.audio_converted += 1
            self.phase = "Converting audio"
            self.progress = max(self.progress, 0.70)
        elif "[Metadata]" in line:
            self.metadata_lines += 1
            self.phase = "Writing metadata"
            self.progress = max(self.progress, 0.80)
        elif "[EmbedThumbnail]" in line:
            self.cover_lines += 1
            self.phase = "Embedding cover"
            self.progress = max(self.progress, 0.84)
        elif "Applying album metadata" in line:
            self.phase = "Applying album metadata"
            self.progress = max(self.progress, 0.88)
        elif "Tagging:" in line:
            self.phase = "Tagging MP3 files"
            self.progress = max(self.progress, 0.90)
        elif "Done." in line:
            self.phase = "Finishing"
            self.progress = max(self.progress, 0.96)

        match = re.search(r"(\d+)\s+new file", line)
        if match:
            self.new_files = int(match.group(1))

    def update_phase(self, phase: str, progress: float, message: str | None = None) -> None:
        with self.lock:
            self.phase = phase
            self.progress = max(self.progress, min(max(progress, 0), 1))
            if message:
                self.message = message

    def succeed(self) -> None:
        with self.lock:
            self.status = "succeeded"
            self.phase = "Done"
            self.message = "Download finished"
            self.progress = 1.0
            self.finished_at = timestamp()

    def fail(self, message: str) -> None:
        with self.lock:
            self.status = "failed"
            self.phase = "Failed"
            self.message = message
            self.error = message
            self.finished_at = timestamp()

    def snapshot(self, base_url: str | None = None) -> dict:
        with self.lock:
            payload = {
                "id": self.id,
                "status": self.status,
                "isActive": self.is_active,
                "phase": self.phase,
                "message": self.message,
                "progress": self.progress,
                "link": self.link,
                "album": self.album,
                "albumArtist": self.album_artist,
                "year": self.year,
                "kind": self.kind,
                "filesStarted": self.files_started,
                "audioConverted": self.audio_converted,
                "metadataLines": self.metadata_lines,
                "coverLines": self.cover_lines,
                "newFiles": self.new_files,
                "reusedFiles": self.reused_files,
                "playlistID": self.playlist_id,
                "playlistTrackCount": self.playlist_track_count,
                "error": self.error,
                "outputTail": list(self.output_tail),
                "createdAt": self.created_at,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
            }

        if base_url:
            payload["statusURL"] = f"{base_url}/api/downloads/{quote(self.id)}"

        return payload


class DownloadManager:
    def __init__(
        self,
        base_dir: Path,
        songs_dir: Path,
        catalog_index: CatalogIndex,
        playlist_manager: "PlaylistManager | None" = None,
    ) -> None:
        self.base_dir = base_dir
        self.songs_dir = songs_dir
        self.catalog_index = catalog_index
        self.playlist_manager = playlist_manager
        self.lock = threading.RLock()
        self.jobs: dict[str, DownloadJob] = {}
        self.active_job_id: str | None = None

    def start(self, payload: dict) -> DownloadJob:
        link = str(payload.get("link") or payload.get("url") or "").strip()
        album = str(payload.get("album") or "").strip()
        album_artist = str(
            payload.get("albumArtist")
            or payload.get("album_artist")
            or payload.get("artist")
            or ""
        ).strip()
        year = str(payload.get("year") or "").strip()
        kind = str(payload.get("kind") or "album").strip().casefold()

        if not link:
            raise DownloadValidationError("Missing YouTube Music link")
        if kind not in {"album", "song", "playlist"}:
            raise DownloadValidationError("Download kind must be album, song, or playlist")
        if kind == "album" and not album:
            raise DownloadValidationError("Missing album name")
        if kind == "album" and not album_artist:
            raise DownloadValidationError("Missing album artist")

        job = DownloadJob(
            link=link,
            album=album,
            album_artist=album_artist,
            year=year,
            kind=kind,
        )

        with self.lock:
            active = self.active_job()
            if active:
                raise DownloadBusyError("Another download is already running")

            self.jobs[job.id] = job
            self.active_job_id = job.id

        thread = threading.Thread(
            target=self.run_job,
            args=(job,),
            name=f"AriaDownload-{job.id}",
            daemon=True,
        )
        thread.start()
        return job

    def active_job(self) -> DownloadJob | None:
        with self.lock:
            if not self.active_job_id:
                return None

            job = self.jobs.get(self.active_job_id)
            if job and job.is_active:
                return job

            return None

    def job(self, job_id: str) -> DownloadJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def recent_jobs(self) -> list[DownloadJob]:
        with self.lock:
            return list(self.jobs.values())[-MAX_DOWNLOAD_HISTORY:]

    def run_job(self, job: DownloadJob) -> None:
        job.mark_running()
        inspected_entries: list[dict] = []
        missing_playlist_items: list[int] = []
        try:
            if job.kind in {"song", "playlist"}:
                job.update_phase("Checking library", 0.06, "Checking for songs already in Aria")
                inspected_entries = self.inspect_entries(job)
                existing_records = self.catalog_index.tracks()
                matched_records = [
                    self.match_entry(entry, existing_records)
                    for entry in inspected_entries
                ]
                job.reused_files = sum(record is not None for record in matched_records)
                missing_playlist_items = [
                    int(entry["playlistIndex"])
                    for entry, record in zip(inspected_entries, matched_records)
                    if record is None
                ]

                legacy_duplicates = self.legacy_duplicate_records(
                    inspected_entries,
                    matched_records,
                    existing_records,
                )
                if legacy_duplicates:
                    job.update_phase(
                        "Cleaning old duplicates",
                        0.1,
                        f"Removing {len(legacy_duplicates)} obsolete playlist duplicate(s)",
                    )
                    deleted_ids = self.catalog_index.delete_track_records(legacy_duplicates)
                    if self.playlist_manager is not None:
                        self.playlist_manager.remove_track_ids(deleted_ids)
                    existing_records = self.catalog_index.tracks()
                    matched_records = [
                        self.match_entry(entry, existing_records)
                        for entry in inspected_entries
                    ]

                if self.refresh_reused_standalone_metadata(
                    job,
                    inspected_entries,
                    matched_records,
                ):
                    existing_records = self.catalog_index.tracks()
                    matched_records = [
                        self.match_entry(entry, existing_records)
                        for entry in inspected_entries
                    ]

                if job.kind == "song" and matched_records and matched_records[0] is not None:
                    job.new_files = 0
                    job.update_phase("Finishing", 0.96, "Song is already in the Aria library")
                    job.succeed()
                    self.clear_active(job)
                    return

                if job.kind == "playlist" and not missing_playlist_items:
                    job.new_files = 0
                    self.create_downloaded_playlist(job, inspected_entries, existing_records)
                    job.succeed()
                    self.clear_active(job)
                    return
        except Exception as error:
            job.fail(str(error))
            self.clear_active(job)
            return

        script_path = self.base_dir / "scripts" / "download.py"
        command = [
            sys.executable,
            str(script_path),
            "--mode",
            job.kind,
            "--link",
            job.link,
        ]
        if job.album:
            command.extend(["--album", job.album])
        if job.album_artist:
            command.extend(["--artist", job.album_artist])
        if job.year:
            command.extend(["--year", job.year])
        if job.kind == "playlist" and missing_playlist_items:
            command.extend(["--playlist-items", ",".join(map(str, missing_playlist_items))])
        environment = dict(os.environ)
        environment.setdefault("PYTHONUNBUFFERED", "1")
        environment.setdefault("TERM", "dumb")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.base_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as error:
            job.fail(f"Could not start downloader: {error}")
            self.clear_active(job)
            return

        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                job.append_output(raw_line)

            return_code = process.wait()
            if return_code == 0:
                job.update_phase("Refreshing catalog", 0.97, "Updating the Aria catalog")
                self.catalog_index.refresh(force=True)
                if job.kind == "playlist":
                    self.create_downloaded_playlist(
                        job,
                        inspected_entries,
                        self.catalog_index.tracks(),
                    )
                job.succeed()
            else:
                job.fail(f"Downloader exited with status {return_code}")
        except Exception as error:
            job.fail(str(error))
        finally:
            self.clear_active(job)

    def inspect_entries(self, job: DownloadJob) -> list[dict]:
        command = [
            downloader_executable(),
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "youtube:player_client=web_embedded",
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--yes-playlist" if job.kind == "playlist" else "--no-playlist",
            job.link,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DownloadValidationError(f"Could not inspect YouTube Music: {error}") from error
        if result.returncode != 0:
            lines = plain_output_line(result.stderr or result.stdout).splitlines()
            detail = lines[-1] if lines else "yt-dlp could not read the link"
            raise DownloadValidationError(f"Could not inspect YouTube Music: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DownloadValidationError("YouTube Music returned invalid playlist information") from error

        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            raw_entries = [payload]
        entries: list[dict] = []
        for fallback_index, raw_entry in enumerate(raw_entries, start=1):
            if not isinstance(raw_entry, dict):
                continue
            video_id = str(raw_entry.get("id") or "").strip()
            title = str(raw_entry.get("title") or "").strip()
            if not video_id or not title:
                continue
            entries.append({
                "id": video_id,
                "title": title,
                "artist": self.entry_artist(raw_entry),
                "playlistIndex": int(
                    raw_entry.get("playlist_index")
                    or raw_entry.get("playlist_autonumber")
                    or fallback_index
                ),
            })
        if not entries:
            raise DownloadValidationError("YouTube Music did not return any downloadable songs")
        return entries[:1] if job.kind == "song" else entries

    @staticmethod
    def entry_artist(entry: dict) -> str:
        artists = entry.get("artists")
        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, dict) and artist.get("name"):
                    return str(artist["name"])
                if isinstance(artist, str) and artist:
                    return artist
        return str(
            entry.get("artist")
            or entry.get("uploader")
            or entry.get("channel")
            or ""
        ).removesuffix(" - Topic").strip()

    @staticmethod
    def match_entry(entry: dict, records: list[dict]) -> dict | None:
        video_id = str(entry.get("id") or "")
        for record in records:
            if youtube_id_from_filename(str(record.get("filename") or "")) == video_id:
                return record

        titles = download_title_identities(entry.get("title"))
        artist = normalized_download_artist(entry.get("artist"))
        title_matches = [
            record
            for record in records
            if normalized_download_identity(record.get("title")) in titles
        ]
        if artist:
            artist_matches = [
                record
                for record in title_matches
                if download_artists_match(record.get("artist"), artist)
                or download_artists_match(record.get("albumArtist"), artist)
            ]
            if artist_matches:
                return artist_matches[0]
            # Older Aria playlist downloads overwrote the original artist with
            # a user-supplied playlist artist. A unique normalized title is
            # still a safer reuse candidate than downloading a duplicate file.
            return title_matches[0] if len(title_matches) == 1 else None
        return title_matches[0] if len(title_matches) == 1 else None

    @staticmethod
    def is_legacy_playlist_record(record: dict) -> bool:
        album = normalized_download_identity(record.get("album"))
        artist = normalized_download_identity(
            record.get("albumArtist") or record.get("artist")
        )
        return album in {"playlist", "youtube music"} or artist == "youtube music"

    @classmethod
    def legacy_duplicate_records(
        cls,
        entries: list[dict],
        matched_records: list[dict | None],
        all_records: list[dict],
    ) -> list[dict]:
        duplicates: dict[str, dict] = {}
        for entry, selected in zip(entries, matched_records):
            if selected is None or cls.is_legacy_playlist_record(selected):
                continue
            selected_id = str(selected.get("id") or "")
            titles = download_title_identities(entry.get("title"))
            for record in all_records:
                record_id = str(record.get("id") or "")
                if not record_id or record_id == selected_id:
                    continue
                if not cls.is_legacy_playlist_record(record):
                    continue
                if normalized_download_identity(record.get("title")) not in titles:
                    continue
                duplicates[record_id] = record
        return list(duplicates.values())

    def refresh_reused_standalone_metadata(
        self,
        job: DownloadJob,
        entries: list[dict],
        matched_records: list[dict | None],
    ) -> bool:
        items: list[dict] = []
        legacy_filenames: set[str] = set()
        for entry, record in zip(entries, matched_records):
            if record is None:
                continue
            is_legacy = self.is_legacy_playlist_record(record)
            if not record.get("isStandalone") and not is_legacy:
                continue
            filename = str(record.get("filename") or "")
            path = self.songs_dir / filename
            if not filename or not path.is_file():
                continue
            items.append({"id": entry["id"], "path": str(path)})
            if is_legacy:
                legacy_filenames.add(filename)

        if not items:
            return False

        job.update_phase(
            "Refreshing song details",
            0.12,
            f"Refreshing individual metadata and covers for {len(items)} existing song(s)",
        )
        refresh_tracks_from_youtube(
            items,
            downloader=downloader_executable(),
            base_dir=self.base_dir,
        )
        if legacy_filenames:
            standalone = standalone_track_filenames(self.songs_dir)
            save_standalone_track_filenames(
                self.songs_dir,
                standalone.union(legacy_filenames),
            )
        self.catalog_index.refresh(force=True)
        return True

    def create_downloaded_playlist(
        self,
        job: DownloadJob,
        entries: list[dict],
        records: list[dict],
    ) -> None:
        if self.playlist_manager is None:
            raise RuntimeError("Playlist storage is unavailable")
        track_ids: list[str] = []
        for entry in entries:
            record = self.match_entry(entry, records)
            if record is None:
                raise RuntimeError(f"Downloaded song could not be added to playlist: {entry['title']}")
            track_ids.append(str(record["id"]))

        playlist_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"aria-youtube-playlist:{job.link}"))
        existing = next(
            (playlist for playlist in self.playlist_manager.all() if playlist["id"] == playlist_id),
            None,
        )
        revision = int(existing.get("revision") or 0) + 1 if existing else 1
        playlist = self.playlist_manager.upsert(
            playlist_id,
            {
                "title": job.album or "YouTube Music Playlist",
                "trackIDs": track_ids,
                "revision": revision,
            },
        )
        job.playlist_id = playlist["id"]
        job.playlist_track_count = len(playlist["trackIDs"])

    def clear_active(self, job: DownloadJob) -> None:
        with self.lock:
            if self.active_job_id == job.id:
                self.active_job_id = None

            if len(self.jobs) > MAX_DOWNLOAD_HISTORY:
                active_id = self.active_job_id
                for old_job in list(self.jobs.values())[:-MAX_DOWNLOAD_HISTORY]:
                    if old_job.id != active_id:
                        self.jobs.pop(old_job.id, None)


class PlaylistManager:
    def __init__(self, songs_dir: Path):
        self.path = songs_dir / ".aria_playlists.json"
        self.lock = threading.RLock()
        self.playlists: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if payload.get("version") != PLAYLIST_STORE_VERSION:
            return

        for item in payload.get("playlists", []):
            if not isinstance(item, dict):
                continue
            try:
                normalized = self._normalized(item, item.get("id"))
            except ValueError:
                continue
            if normalized is not None:
                self.playlists[normalized["id"]] = normalized

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(item) for item in self.playlists.values()]

    def upsert(self, playlist_id: str, payload: dict) -> dict:
        normalized = self._normalized(payload, playlist_id)
        if normalized is None:
            raise ValueError("Invalid playlist payload")

        with self.lock:
            existing = self.playlists.get(playlist_id)
            if existing is not None and existing.get("revision", 0) > normalized["revision"]:
                return dict(existing)
            self.playlists[playlist_id] = normalized
            self._save()
            return dict(normalized)

    def delete(self, playlist_id: str) -> bool:
        with self.lock:
            if playlist_id not in self.playlists:
                return False
            del self.playlists[playlist_id]
            self._save()
            return True

    def remove_track_ids(self, track_ids: set[str]) -> int:
        if not track_ids:
            return 0

        changed_count = 0
        with self.lock:
            for playlist_id, playlist in list(self.playlists.items()):
                remaining_ids = [
                    track_id
                    for track_id in playlist.get("trackIDs", [])
                    if track_id not in track_ids
                ]
                if len(remaining_ids) == len(playlist.get("trackIDs", [])):
                    continue
                updated = dict(playlist)
                updated["trackIDs"] = remaining_ids
                updated["revision"] = int(updated.get("revision") or 0) + 1
                self.playlists[playlist_id] = updated
                changed_count += 1
            if changed_count:
                self._save()
        return changed_count

    def _normalized(self, payload: dict, playlist_id: str | None) -> dict | None:
        try:
            normalized_id = str(uuid.UUID(str(playlist_id)))
        except (ValueError, TypeError, AttributeError):
            return None

        title = str(payload.get("title") or "Untitled Playlist").strip()[:120]
        if not title:
            title = "Untitled Playlist"

        raw_track_ids = payload.get("trackIDs", [])
        if not isinstance(raw_track_ids, list):
            return None

        track_ids: list[str] = []
        seen: set[str] = set()
        for raw_track_id in raw_track_ids[:10_000]:
            try:
                track_id = str(uuid.UUID(str(raw_track_id)))
            except (ValueError, TypeError, AttributeError):
                continue
            if track_id not in seen:
                seen.add(track_id)
                track_ids.append(track_id)

        cover_data = payload.get("coverImageData")
        if cover_data is not None:
            if not isinstance(cover_data, str):
                return None
            try:
                decoded_cover = base64.b64decode(cover_data, validate=True)
            except (ValueError, TypeError):
                return None
            if len(decoded_cover) > MAX_PLAYLIST_COVER_BYTES:
                raise ValueError("Playlist cover is too large")

        return {
            "id": normalized_id,
            "title": title,
            "trackIDs": track_ids,
            "coverImageData": cover_data,
            "revision": max(int(payload.get("revision") or 0), 0),
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "version": PLAYLIST_STORE_VERSION,
                    "playlists": list(self.playlists.values()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)


class AriaSongHandler(BaseHTTPRequestHandler):
    server_version = f"AriaSongServer/{ARIA_VERSION}"

    @property
    def songs_dir(self) -> Path:
        return self.server.songs_dir

    @property
    def catalog_index(self) -> CatalogIndex:
        return self.server.catalog_index

    @property
    def download_manager(self) -> DownloadManager:
        return self.server.download_manager

    @property
    def lyrics_manager(self) -> LyricsManager:
        return self.server.lyrics_manager

    @property
    def playlist_manager(self) -> PlaylistManager:
        return self.server.playlist_manager

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS, PATCH, PUT, POST, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_PATCH(self) -> None:
        self.handle_track_update()

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/playlists/"):
            self.upsert_playlist(parsed)
        else:
            self.handle_track_update()

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/playlists/"):
            self.delete_playlist(parsed)
        elif parsed.path.startswith("/api/tracks/") and parsed.path.endswith("/album"):
            self.delete_track_album(parsed)
        elif parsed.path.startswith("/api/albums/"):
            self.delete_album(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/downloads":
            self.start_download()
        elif parsed.path.startswith("/api/tracks/") and parsed.path.endswith("/artwork/refresh"):
            self.refresh_track_artwork(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.write_text(
                f"Aria song server {ARIA_VERSION} is running.\n"
                "Try /api/tracks?offset=0&limit=100\n"
            )
        elif parsed.path == "/api/tracks":
            self.write_tracks(parsed)
        elif parsed.path.startswith("/api/tracks/") and parsed.path.endswith("/lyrics"):
            self.write_lyrics(parsed)
        elif parsed.path.startswith("/api/tracks/"):
            self.write_track(parsed)
        elif parsed.path == "/api/search":
            self.write_search(parsed)
        elif parsed.path == "/api/albums":
            self.write_albums(parsed)
        elif parsed.path.startswith("/api/albums/") and parsed.path.endswith("/tracks"):
            album_id = parsed.path.removeprefix("/api/albums/").removesuffix("/tracks")
            self.write_album_tracks(unquote(album_id), parsed)
        elif parsed.path == "/api/catalog":
            self.write_catalog_summary()
        elif parsed.path == "/api/playlists":
            self.write_json(self.playlist_manager.all())
        elif parsed.path == "/api/downloads":
            self.write_downloads()
        elif parsed.path.startswith("/api/downloads/"):
            self.write_download(unquote(parsed.path.removeprefix("/api/downloads/")).strip("/"))
        elif parsed.path.startswith("/api/stream/"):
            self.stream_song(parsed.path.removeprefix("/api/stream/"))
        elif parsed.path.startswith("/api/artwork/"):
            self.write_artwork(parsed.path.removeprefix("/api/artwork/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def upsert_playlist(self, parsed) -> None:
        playlist_id = unquote(parsed.path.removeprefix("/api/playlists/")).strip("/")
        payload = self.read_json_body()
        if payload is None:
            return

        try:
            playlist = self.playlist_manager.upsert(playlist_id, payload)
        except ValueError as error:
            self.write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        self.write_json(playlist)

    def delete_playlist(self, parsed) -> None:
        playlist_id = unquote(parsed.path.removeprefix("/api/playlists/")).strip("/")
        try:
            playlist_id = str(uuid.UUID(playlist_id))
        except (ValueError, TypeError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid playlist id")
            return

        if not self.playlist_manager.delete(playlist_id):
            self.send_error(HTTPStatus.NOT_FOUND, "Playlist not found")
            return

        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.end_headers()

    def delete_album(self, parsed) -> None:
        album_id = unquote(parsed.path.removeprefix("/api/albums/")).strip("/")
        if not album_id:
            self.write_json({"error": "Missing album id"}, status=HTTPStatus.BAD_REQUEST)
            return
        if self.download_manager.active_job() is not None:
            self.write_json(
                {"error": "Wait for the active music download to finish before deleting an album."},
                status=HTTPStatus.CONFLICT,
            )
            return

        try:
            result = self.catalog_index.delete_album(album_id)
        except OSError as error:
            self.write_json(
                {"error": f"Could not delete album files: {error}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if result is None:
            self.write_json({"error": "Album not found"}, status=HTTPStatus.NOT_FOUND)
            return

        deleted_files, deleted_track_ids = result
        updated_playlists = self.playlist_manager.remove_track_ids(deleted_track_ids)
        self.write_json({
            "deletedFiles": deleted_files,
            "deletedTrackIDs": sorted(deleted_track_ids),
            "updatedPlaylists": updated_playlists,
        })

    def delete_track_album(self, parsed) -> None:
        track_id = unquote(
            parsed.path.removeprefix("/api/tracks/").removesuffix("/album")
        ).strip("/")
        record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.catalog_index.refresh(force=True)
            record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.write_json({"error": "Track not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if bool(record.get("isStandalone")):
            self.write_json(
                {"error": "Standalone songs do not belong to a deletable album."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        class AlbumPath:
            path = f"/api/albums/{quote(str(record.get('albumID') or ''))}"

        self.delete_album(AlbumPath())

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/stream/"):
            self.stream_song(parsed.path.removeprefix("/api/stream/"), send_body=False)
        else:
            self.send_response(HTTPStatus.OK)
            self.end_headers()

    def write_track(self, parsed) -> None:
        raw_track_path = parsed.path.removeprefix("/api/tracks/")
        wants_metadata = False

        if raw_track_path.endswith("/metadata"):
            wants_metadata = True
            raw_track_path = raw_track_path.removesuffix("/metadata")

        track_id = unquote(raw_track_path).strip("/")
        if not track_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing track id")
            return

        record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())
        track = track_payload_from_record(
            record,
            base_url,
            artwork_by_album.get(album_key_for_record(record)),
        )

        metadata = {
            "id": track["id"],
            "title": track["title"],
            "artist": track["artist"],
            "albumArtist": track["albumArtist"],
            "album": track["album"],
            "year": track["year"],
            "trackNumber": track["trackNumber"],
            "duration": track["duration"],
            "isExplicit": track["isExplicit"],
            "artwork": track["artwork"],
            "artworkURL": track["artworkURL"],
            "streamURL": track["streamURL"],
        }

        if wants_metadata:
            self.write_json({
                **metadata,
                "metadata": metadata,
                "track": track,
            })
            return

        self.write_json({
            **track,
            "metadata": metadata,
        })

    def write_lyrics(self, parsed) -> None:
        track_id = unquote(
            parsed.path.removeprefix("/api/tracks/").removesuffix("/lyrics")
        ).strip("/")
        if not track_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing track id")
            return

        record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        self.write_json(self.lyrics_manager.lyrics_for(record))

    def refresh_track_artwork(self, parsed) -> None:
        track_id = unquote(
            parsed.path.removeprefix("/api/tracks/").removesuffix("/artwork/refresh")
        ).strip("/")
        if not track_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing track id")
            return

        if self.download_manager.active_job() is not None:
            self.write_json(
                {"error": "Wait for the active music download to finish before refreshing artwork."},
                status=HTTPStatus.CONFLICT,
            )
            return

        if self.catalog_index.track_for_id(track_id) is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        payload = self.read_json_body()
        if payload is None:
            return

        source_url = str(payload.get("sourceURL") or payload.get("sourceUrl") or "").strip()
        if not source_url:
            self.write_json(
                {"error": "Missing YouTube artwork URL."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            artwork_data, artwork_mime = download_youtube_artwork(source_url)
            refresh_result = self.catalog_index.refresh_album_artwork(
                track_id,
                artwork_data,
                artwork_mime,
            )
        except ArtworkRefreshError as error:
            self.write_json({"error": str(error)}, status=error.status)
            return
        except Exception as error:
            self.write_json(
                {"error": f"Artwork refresh failed: {error}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if refresh_result is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        updated_record, refreshed_track_count = refresh_result
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())
        response = track_payload_from_record(
            updated_record,
            base_url,
            artwork_by_album.get(album_key_for_record(updated_record)),
        )
        response["refreshedTrackCount"] = refreshed_track_count
        response["artworkSource"] = "YouTube Music"
        self.write_json(response)

    def handle_track_update(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/tracks/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        track_id = unquote(parsed.path.removeprefix("/api/tracks/")).strip()
        if not track_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing track id")
            return

        payload = self.read_json_body()
        if payload is None:
            return

        updates = self.track_metadata_updates(payload)
        if updates is None:
            return

        updated_record = self.catalog_index.update_track_metadata(track_id, updates)
        if updated_record is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())
        self.write_json(
            track_payload_from_record(
                updated_record,
                base_url,
                artwork_by_album.get(album_key_for_record(updated_record)),
            )
        )

    def read_json_body(self) -> dict | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None

        if content_length <= 0:
            return {}

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected a JSON request body")
            return None

        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
            return None

        return payload

    def start_download(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"

        try:
            job = self.download_manager.start(payload)
        except DownloadValidationError as error:
            self.write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except DownloadBusyError as error:
            active_job = self.download_manager.active_job()
            self.write_json(
                {
                    "error": str(error),
                    "active": active_job.snapshot(base_url) if active_job else None,
                },
                status=HTTPStatus.CONFLICT,
            )
            return

        self.write_json(job.snapshot(base_url), status=HTTPStatus.ACCEPTED)

    def write_downloads(self) -> None:
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        active_job = self.download_manager.active_job()
        self.write_json({
            "active": active_job.snapshot(base_url) if active_job else None,
            "jobs": [
                job.snapshot(base_url)
                for job in reversed(self.download_manager.recent_jobs())
            ],
        })

    def write_download(self, job_id: str) -> None:
        if not job_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing download id")
            return

        job = self.download_manager.job(job_id)
        if job is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Download not found")
            return

        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        self.write_json(job.snapshot(base_url))

    def track_metadata_updates(self, payload: dict) -> dict | None:
        source = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else payload
        updates: dict = {}

        text_fields = ("title", "artist", "albumArtist", "album")
        for field in text_fields:
            if field not in source:
                continue

            value = source.get(field)
            if value is None:
                continue

            text_value = str(value).strip()
            if field in ("title", "artist") and not text_value:
                self.send_error(HTTPStatus.BAD_REQUEST, f"{field} cannot be empty")
                return None

            if field == "album" and not text_value:
                text_value = "Fedora songs"

            if field == "albumArtist" and not text_value:
                text_value = str(source.get("artist") or updates.get("artist") or "Unknown Artist").strip()

            updates[field] = text_value

        if "year" in source:
            year = source.get("year")
            if year in (None, ""):
                updates["year"] = datetime.now().year
            else:
                try:
                    updates["year"] = int(year)
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "year must be a number")
                    return None

        if "trackNumber" in source:
            track_number = source.get("trackNumber")
            if track_number in (None, ""):
                updates["trackNumber"] = None
            else:
                try:
                    updates["trackNumber"] = int(track_number)
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "trackNumber must be a number")
                    return None

                if updates["trackNumber"] < 1:
                    self.send_error(HTTPStatus.BAD_REQUEST, "trackNumber must be 1 or greater")
                    return None

        if "isExplicit" in source:
            updates["isExplicit"] = bool(source.get("isExplicit"))

        if not updates:
            self.send_error(
                HTTPStatus.BAD_REQUEST,
                "No supported metadata fields found. Send title, artist, albumArtist, album, year, trackNumber, or isExplicit.",
            )
            return None

        return updates

    def write_tracks(self, parsed) -> None:
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        records = self.filtered_track_records(parsed)
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())

        payload = [
            track_payload_from_record(
                record,
                base_url,
                artwork_by_album.get(album_key_for_record(record)),
            )
            for record in records
        ]

        if not parsed.query:
            self.write_json(payload)
            return

        params = parse_qs(parsed.query)
        offset, limit = self.pagination(params)
        page = paged_payload(payload, offset, limit)
        page["tracks"] = page["items"]
        page["query"] = query_text(params)
        self.write_json(page)

    def write_search(self, parsed) -> None:
        params = parse_qs(parsed.query)
        query = query_text(params)
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        records = self.filtered_track_records(parsed)
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())
        tracks = [
            track_payload_from_record(
                record,
                base_url,
                artwork_by_album.get(album_key_for_record(record)),
            )
            for record in records
        ]

        offset, limit = self.pagination(params)
        track_page = paged_payload(tracks, offset, limit)
        album_matches = [
            album
            for album in self.album_summaries(base_url)
            if not query or all(token in album["searchText"] for token in query.casefold().split())
        ]

        track_page["tracks"] = track_page["items"]
        self.write_json({
            "query": query,
            "tracks": track_page,
            "albums": [self.public_album_summary(album) for album in album_matches[:25]],
            "albumTotal": len(album_matches),
        })

    def write_albums(self, parsed) -> None:
        params = parse_qs(parsed.query)
        query = query_text(params)
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        albums = [
            album
            for album in self.album_summaries(base_url)
            if not query or all(token in album["searchText"] for token in query.casefold().split())
        ]
        offset, limit = self.pagination(params)
        page = paged_payload([self.public_album_summary(album) for album in albums], offset, limit)
        page["albums"] = page["items"]
        page["query"] = query
        self.write_json(page)

    def write_album_tracks(self, album_id: str, parsed) -> None:
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        records = [
            record
            for record in self.catalog_index.tracks()
            if record.get("albumID") == album_id
        ]
        records.sort(key=track_sort_key)

        if not records:
            self.send_error(HTTPStatus.NOT_FOUND, "Album not found")
            return

        artwork_by_album = album_artwork_sources(records)
        payload = [
            track_payload_from_record(
                record,
                base_url,
                artwork_by_album.get(album_key_for_record(record)),
            )
            for record in records
        ]
        params = parse_qs(parsed.query)
        offset, limit = self.pagination(params)
        page = paged_payload(payload, offset, limit)
        page["tracks"] = page["items"]
        page["album"] = self.public_album_summary(self.album_summary_for_records(base_url, records))
        self.write_json(page)

    def write_catalog_summary(self) -> None:
        records = self.catalog_index.tracks()
        albums = self.album_summaries(f"http://{self.headers.get('Host', 'localhost:8000')}")
        index_status = self.catalog_index.status()
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        active_job = self.download_manager.active_job()
        self.write_json({
            "ariaVersion": ARIA_VERSION,
            "trackCount": len(records),
            "albumCount": len(albums),
            "indexVersion": CATALOG_INDEX_VERSION,
            "isIndexing": index_status["isIndexing"],
            "lastIndexError": index_status["lastError"],
            "activeDownload": active_job.snapshot(base_url) if active_job else None,
        })

    def filtered_track_records(self, parsed) -> list[dict]:
        params = parse_qs(parsed.query)
        query = query_text(params)
        records = [
            record
            for record in self.catalog_index.tracks()
            if matches_query(record, query)
        ]
        records.sort(key=title_sort_key)
        return records

    def pagination(self, params: dict[str, list[str]]) -> tuple[int, int]:
        offset = query_int(params, "offset", 0, 0, 10_000_000)
        limit = query_int(params, "limit", DEFAULT_PAGE_LIMIT, 1, MAX_PAGE_LIMIT)
        return offset, limit

    def album_summaries(self, base_url: str) -> list[dict]:
        grouped: dict[str, list[dict]] = {}

        for record in self.catalog_index.tracks():
            if bool(record.get("isStandalone")):
                continue
            grouped.setdefault(str(record.get("albumID")), []).append(record)

        albums = [
            self.album_summary_for_records(base_url, records)
            for records in grouped.values()
            if records
        ]
        albums.sort(key=lambda album: (album["title"].casefold(), album["artist"].casefold()))
        return albums

    def album_summary_for_records(self, base_url: str, records: list[dict]) -> dict:
        records = sorted(records, key=track_sort_key)
        first_record = records[0]
        artwork_record = next((record for record in records if record.get("hasArtwork")), None)
        artwork_url = artwork_url_for_record(artwork_record, base_url)

        title = first_record.get("album") or "Fedora songs"
        artist = album_artist_for_records(records)
        album_id = first_record.get("albumID") or album_id_for(title)
        track_artists = " ".join(
            sorted(
                {
                    str(record.get("artist") or "")
                    for record in records
                    if str(record.get("artist") or "")
                },
                key=str.casefold,
            )
        )

        return {
            "id": album_id,
            "title": title,
            "artist": artist,
            "year": min(record.get("year") or datetime.now().year for record in records),
            "trackCount": len(records),
            "duration": sum(float(record.get("duration") or 0) for record in records),
            "artwork": first_record.get("artwork"),
            "artworkURL": artwork_url,
            "tracksURL": f"{base_url}/api/albums/{quote(album_id)}/tracks",
            "searchText": f"{title} {artist} {track_artists}".casefold(),
        }

    def public_album_summary(self, album: dict) -> dict:
        return {
            key: value
            for key, value in album.items()
            if key != "searchText"
        }

    def write_json(self, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")

        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def album_artwork_urls(self, files: list[Path], metadata_by_path: dict[Path, dict], base_url: str) -> dict[str, str]:
        artwork_by_album: dict[str, str] = {}

        for path in files:
            metadata = metadata_by_path[path]
            if not metadata.get("hasArtwork"):
                continue

            key = album_key(path, metadata)
            if key not in artwork_by_album:
                artwork_by_album[key] = f"{base_url}/api/artwork/{quote(path.name)}"

        return artwork_by_album

    def write_artwork(self, raw_name: str) -> None:
        name = unquote(raw_name)
        path = (self.songs_dir / name).resolve()

        if path.parent != self.songs_dir.resolve() or not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Artwork not found")
            return

        body = embedded_artwork(path)
        if body is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Artwork not found")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_song(self, raw_name: str, send_body: bool = True) -> None:
        name = unquote(raw_name)
        path = (self.songs_dir / name).resolve()

        if path.parent != self.songs_dir.resolve() or not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Song not found")
            return

        file_size = path.stat().st_size
        start, end = self.byte_range(file_size)
        content_length = end - start + 1
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        self.send_response(HTTPStatus.PARTIAL_CONTENT if self.headers.get("Range") else HTTPStatus.OK)
        self.send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))

        if self.headers.get("Range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")

        self.end_headers()

        if not send_body:
            return

        with path.open("rb") as file:
            file.seek(start)
            remaining = content_length

            while remaining > 0:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break

                self.wfile.write(chunk)
                remaining -= len(chunk)

    def byte_range(self, file_size: int) -> tuple[int, int]:
        header = self.headers.get("Range")
        if not header:
            return 0, file_size - 1

        match = re.match(r"bytes=(\d*)-(\d*)", header)
        if not match:
            return 0, file_size - 1

        start_text, end_text = match.groups()
        if not start_text and end_text:
            suffix_length = int(end_text)
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else file_size - 1

        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        return start, end

    def write_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_common_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS, PATCH, PUT, POST, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve local songs to the Aria iOS app.")
    parser.add_argument(
        "--songs-dir",
        default=os.environ.get("ARIA_SONGS_DIR", str(default_songs_dir())),
        help="Directory containing songs. Defaults to ../songs next to this server folder.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ARIA_SERVER_HOST", "0.0.0.0"),
        help="Bind host. Defaults to 0.0.0.0 so phones on the LAN can connect.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ARIA_SERVER_PORT", "8000")),
        help="Bind port. Defaults to 8000.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    songs_dir = Path(args.songs_dir).expanduser().resolve()
    songs_dir.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), AriaSongHandler)
    server.songs_dir = songs_dir
    server.catalog_index = CatalogIndex(songs_dir)
    server.lyrics_manager = LyricsManager(songs_dir)
    server.playlist_manager = PlaylistManager(songs_dir)
    server.download_manager = DownloadManager(
        Path(__file__).resolve().parent.parent,
        songs_dir,
        server.catalog_index,
        server.playlist_manager,
    )
    server.catalog_index.refresh_in_background(force=True)

    print(f"Serving {songs_dir} at http://{args.host}:{args.port}")
    print(f"Catalog cache: {server.catalog_index.index_path}")
    print(f"Lyrics cache: {server.lyrics_manager.cache_dir}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Aria song server.")


if __name__ == "__main__":
    main()
