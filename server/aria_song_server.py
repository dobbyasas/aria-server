#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, quote, unquote, urlparse

from mutagen import File as MutagenFile


SONG_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac"}
CATALOG_INDEX_VERSION = 2
CATALOG_REFRESH_INTERVAL_SECONDS = 10
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
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


class MetadataUpdateError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


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


def ffprobe_metadata(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:format_tags=title,artist,album,date,track,tracknumber:stream=codec_type:stream_disposition=attached_pic",
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


def palette_for(path: Path) -> dict:
    palette_index = uuid.uuid5(uuid.NAMESPACE_URL, path.name).int % len(PALETTES)
    top_hex, bottom_hex, symbol_name = PALETTES[palette_index]
    return {
        "topHex": top_hex,
        "bottomHex": bottom_hex,
        "symbolName": symbol_name,
    }


def album_key(path: Path, metadata: dict) -> str:
    fallback_title, fallback_artist = title_from_filename(path)
    artist = metadata.get("artist") or fallback_artist
    album = metadata.get("album") or "Fedora songs"

    return f"{artist.casefold()}::{album.casefold()}"


def track_payload(path: Path, base_url: str, metadata: dict, artwork_url: str | None) -> dict:
    fallback_title, fallback_artist = title_from_filename(path)
    filename = quote(path.name)

    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path.name)),
        "title": metadata.get("title") or fallback_title,
        "artist": metadata.get("artist") or fallback_artist,
        "album": metadata.get("album") or "Fedora songs",
        "duration": metadata.get("duration") or 0,
        "year": year_from_date(metadata.get("date")),
        "trackNumber": metadata.get("trackNumber"),
        "artwork": palette_for(path),
        "streamURL": f"{base_url}/api/stream/{filename}",
        "artworkURL": artwork_url,
        "isExplicit": False,
    }


def build_track_record(path: Path, metadata: dict) -> dict:
    fallback_title, fallback_artist = title_from_filename(path)
    stat = path.stat()

    title = metadata.get("title") or fallback_title
    artist = metadata.get("artist") or fallback_artist
    album = metadata.get("album") or "Fedora songs"

    record = {
        "filename": path.name,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path.name)),
        "title": title,
        "artist": artist,
        "album": album,
        "duration": metadata.get("duration") or 0,
        "year": year_from_date(metadata.get("date")),
        "trackNumber": metadata.get("trackNumber"),
        "artwork": palette_for(path),
        "hasArtwork": bool(metadata.get("hasArtwork")),
        "isExplicit": False,
    }
    record["searchText"] = search_text_for(record)
    record["albumID"] = album_id_for(artist, album)
    return record


def search_text_for(record: dict) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "artist", "album", "filename")
    ).casefold()


def album_id_for(artist: str, album: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aria-album:{artist.casefold()}::{album.casefold()}"))


def album_key_for_record(record: dict) -> str:
    return f"{str(record.get('artist') or '').casefold()}::{str(record.get('album') or '').casefold()}"


def track_sort_key(record: dict) -> tuple:
    return (
        str(record.get("artist") or "").casefold(),
        str(record.get("album") or "").casefold(),
        record.get("trackNumber") if record.get("trackNumber") is not None else 999_999,
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


def track_payload_from_record(record: dict, base_url: str, artwork_source_filename: str | None = None) -> dict:
    filename = quote(str(record["filename"]))
    artwork_url = f"{base_url}/api/artwork/{quote(artwork_source_filename)}" if artwork_source_filename else None

    return {
        "id": record["id"],
        "title": record.get("title") or Path(record["filename"]).stem,
        "artist": record.get("artist") or "Unknown Artist",
        "album": record.get("album") or "Fedora songs",
        "duration": record.get("duration") or 0,
        "year": record.get("year") or datetime.now().year,
        "trackNumber": record.get("trackNumber"),
        "artwork": record.get("artwork") or palette_for(Path(record["filename"])),
        "streamURL": f"{base_url}/api/stream/{filename}",
        "artworkURL": artwork_url,
        "isExplicit": bool(record.get("isExplicit")),
    }


def album_artwork_sources(records: list[dict]) -> dict[str, str]:
    artwork_by_album: dict[str, str] = {}

    for record in records:
        if not record.get("hasArtwork"):
            continue

        key = album_key_for_record(record)
        if key not in artwork_by_album:
            artwork_by_album[key] = record["filename"]

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


def cleaned_metadata_text(value, field: str, fallback: str | None = None) -> str:
    if value is None:
        if fallback is not None:
            return fallback
        raise MetadataUpdateError(f"{field} is required.")

    text = str(value).strip()
    if text:
        return text

    if fallback is not None:
        return fallback

    raise MetadataUpdateError(f"{field} is required.")


def optional_metadata_int(value, field: str, minimum: int = 0) -> int | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = int(text)
    except ValueError as error:
        raise MetadataUpdateError(f"{field} must be a whole number.") from error

    if parsed < minimum:
        raise MetadataUpdateError(f"{field} must be at least {minimum}.")

    return parsed


def metadata_bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", ""}:
            return False

    raise MetadataUpdateError(f"{field} must be true or false.")


def metadata_updates_from_payload(payload) -> dict:
    if not isinstance(payload, dict):
        raise MetadataUpdateError("Expected a JSON object.")

    updates: dict = {}

    if "title" in payload:
        updates["title"] = cleaned_metadata_text(payload.get("title"), "title")
    if "artist" in payload:
        updates["artist"] = cleaned_metadata_text(payload.get("artist"), "artist", "Unknown Artist")
    if "album" in payload:
        updates["album"] = cleaned_metadata_text(payload.get("album"), "album", "Fedora songs")
    if "year" in payload:
        updates["year"] = optional_metadata_int(payload.get("year"), "year")
    if "trackNumber" in payload:
        updates["trackNumber"] = optional_metadata_int(payload.get("trackNumber"), "trackNumber", minimum=1)
    if "track_number" in payload and "trackNumber" not in updates:
        updates["trackNumber"] = optional_metadata_int(payload.get("track_number"), "track_number", minimum=1)
    if "isExplicit" in payload:
        updates["isExplicit"] = metadata_bool(payload.get("isExplicit"), "isExplicit")
    if "explicit" in payload and "isExplicit" not in updates:
        updates["isExplicit"] = metadata_bool(payload.get("explicit"), "explicit")

    return updates


def write_audio_metadata(path: Path, updates: dict) -> None:
    writable_fields = {"title", "artist", "album", "year", "trackNumber"}
    if not any(field in updates for field in writable_fields):
        return

    try:
        audio = MutagenFile(path, easy=True)
    except Exception as error:
        raise MetadataUpdateError(
            f"Could not read metadata for {path.name}: {error}",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ) from error

    if audio is None:
        raise MetadataUpdateError(
            f"{path.name} uses an unsupported audio metadata format.",
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        )

    try:
        if audio.tags is None:
            audio.add_tags()

        if "title" in updates:
            audio["title"] = [updates["title"]]
        if "artist" in updates:
            audio["artist"] = [updates["artist"]]
        if "album" in updates:
            audio["album"] = [updates["album"]]
        if "year" in updates:
            if updates["year"] is None:
                audio.pop("date", None)
            else:
                audio["date"] = [str(updates["year"])]
        if "trackNumber" in updates:
            if updates["trackNumber"] is None:
                audio.pop("tracknumber", None)
            else:
                audio["tracknumber"] = [str(updates["trackNumber"])]

        audio.save()
    except Exception as error:
        raise MetadataUpdateError(
            f"Could not save metadata for {path.name}: {error}",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from error


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
                if record.get("id") == track_id or record.get("filename") == track_id:
                    return dict(record)

        return None

    def update_track_metadata(self, track_id: str, updates: dict, path: Path) -> dict | None:
        stat = path.stat()
        updated_record = None
        records_snapshot: list[dict] = []

        with self.lock:
            for index, record in enumerate(self.records):
                if record.get("id") != track_id and record.get("filename") != track_id:
                    continue

                updated = dict(record)
                for field in ("title", "artist", "album", "year", "trackNumber", "isExplicit"):
                    if field in updates:
                        updated[field] = updates[field] if updates[field] is not None else 0 if field == "year" else None

                updated["size"] = stat.st_size
                updated["mtimeNs"] = stat.st_mtime_ns
                updated["searchText"] = search_text_for(updated)
                updated["albumID"] = album_id_for(
                    updated.get("artist") or "Unknown Artist",
                    updated.get("album") or "Fedora songs",
                )
                updated_record = self.normalized_record(updated)
                self.records[index] = updated_record
                break

            if updated_record is None:
                return None

            self.records.sort(key=title_sort_key)
            self.records_by_filename = {record["filename"]: record for record in self.records}
            self.last_refresh_at = monotonic()
            records_snapshot = [dict(record) for record in self.records]

        self.save(records_snapshot)
        return dict(updated_record)

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
        record.setdefault("albumID", album_id_for(record.get("artist") or "", record.get("album") or ""))
        record.setdefault("hasArtwork", False)
        record.setdefault("isExplicit", False)
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
                record = build_track_record(path, metadata)
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


class AriaSongHandler(BaseHTTPRequestHandler):
    server_version = "AriaSongServer/0.2"

    @property
    def songs_dir(self) -> Path:
        return self.server.songs_dir

    @property
    def catalog_index(self) -> CatalogIndex:
        return self.server.catalog_index

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.write_text("Aria song server is running.\nTry /api/tracks?offset=0&limit=100\n")
        elif parsed.path == "/api/tracks":
            self.write_tracks(parsed)
        elif parsed.path.startswith("/api/tracks/"):
            self.write_track(parsed.path.removeprefix("/api/tracks/"))
        elif parsed.path == "/api/search":
            self.write_search(parsed)
        elif parsed.path == "/api/albums":
            self.write_albums(parsed)
        elif parsed.path.startswith("/api/albums/") and parsed.path.endswith("/tracks"):
            album_id = parsed.path.removeprefix("/api/albums/").removesuffix("/tracks")
            self.write_album_tracks(unquote(album_id), parsed)
        elif parsed.path == "/api/catalog":
            self.write_catalog_summary()
        elif parsed.path.startswith("/api/stream/"):
            self.stream_song(parsed.path.removeprefix("/api/stream/"))
        elif parsed.path.startswith("/api/artwork/"):
            self.write_artwork(parsed.path.removeprefix("/api/artwork/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:
        self.update_track()

    def do_PUT(self) -> None:
        self.update_track()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/stream/"):
            self.stream_song(parsed.path.removeprefix("/api/stream/"), send_body=False)
        else:
            self.send_response(HTTPStatus.OK)
            self.end_headers()

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

    def write_track(self, raw_track_id: str) -> None:
        track_id = unquote(raw_track_id)
        record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.write_json_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        self.write_json(self.track_payload(record))

    def update_track(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/tracks/"):
            self.write_json_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        track_id = unquote(parsed.path.removeprefix("/api/tracks/"))
        record = self.catalog_index.track_for_id(track_id)
        if record is None:
            self.write_json_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        path = (self.songs_dir / str(record["filename"])).resolve()
        if path.parent != self.songs_dir.resolve() or not path.exists() or not path.is_file():
            self.write_json_error(HTTPStatus.NOT_FOUND, "Song file not found")
            return

        try:
            payload = self.read_json_body()
            updates = metadata_updates_from_payload(payload)
            write_audio_metadata(path, updates)
            updated_record = self.catalog_index.update_track_metadata(str(record["id"]), updates, path)
        except MetadataUpdateError as error:
            self.write_json_error(error.status, str(error))
            return
        except Exception as error:
            self.write_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not update metadata: {error}")
            return

        if updated_record is None:
            self.write_json_error(HTTPStatus.NOT_FOUND, "Track not found")
            return

        self.write_json(self.track_payload(updated_record))

    def read_json_body(self) -> dict:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError as error:
            raise MetadataUpdateError("Content-Length must be a number.") from error

        if content_length <= 0:
            return {}
        if content_length > 64 * 1024:
            raise MetadataUpdateError("Request body is too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        raw_body = self.rfile.read(content_length)
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MetadataUpdateError("Request body must be valid JSON.") from error

    def track_payload(self, record: dict) -> dict:
        base_url = f"http://{self.headers.get('Host', 'localhost:8000')}"
        artwork_by_album = album_artwork_sources(self.catalog_index.tracks())
        return track_payload_from_record(
            record,
            base_url,
            artwork_by_album.get(album_key_for_record(record)),
        )

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
        self.write_json({
            "trackCount": len(records),
            "albumCount": len(albums),
            "indexVersion": CATALOG_INDEX_VERSION,
            "isIndexing": index_status["isIndexing"],
            "lastIndexError": index_status["lastError"],
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
            grouped.setdefault(str(record.get("albumID")), []).append(record)

        albums = [
            self.album_summary_for_records(base_url, records)
            for records in grouped.values()
            if records
        ]
        albums.sort(key=lambda album: (album["artist"].casefold(), album["title"].casefold()))
        return albums

    def album_summary_for_records(self, base_url: str, records: list[dict]) -> dict:
        records = sorted(records, key=track_sort_key)
        first_record = records[0]
        artwork_record = next((record for record in records if record.get("hasArtwork")), None)
        artwork_url = (
            f"{base_url}/api/artwork/{quote(artwork_record['filename'])}"
            if artwork_record
            else None
        )

        title = first_record.get("album") or "Fedora songs"
        artist = first_record.get("artist") or "Unknown Artist"

        return {
            "id": first_record.get("albumID") or album_id_for(artist, title),
            "title": title,
            "artist": artist,
            "year": min(record.get("year") or datetime.now().year for record in records),
            "trackCount": len(records),
            "duration": sum(float(record.get("duration") or 0) for record in records),
            "artwork": first_record.get("artwork"),
            "artworkURL": artwork_url,
            "tracksURL": f"{base_url}/api/albums/{quote(first_record.get('albumID') or album_id_for(artist, title))}/tracks",
            "searchText": f"{title} {artist}".casefold(),
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

    def write_json_error(self, status: HTTPStatus, message: str) -> None:
        self.write_json({"error": message}, status=status)

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
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS, PATCH, PUT")
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
    server.catalog_index.refresh_in_background(force=True)

    print(f"Serving {songs_dir} at http://{args.host}:{args.port}")
    print(f"Catalog cache: {server.catalog_index.index_path}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Aria song server.")


if __name__ == "__main__":
    main()
