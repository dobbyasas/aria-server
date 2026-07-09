# Aria Song Server

Run this on the Fedora laptop from `~/aria-server`:

```sh
python3 server/aria_song_server.py
```

The server reads songs from `~/aria-server/songs`, keeps a cached catalog index at
`~/aria-server/songs/.aria_catalog_index.json`, and exposes:

- `GET /api/catalog` for track/album counts and index version
- `GET /api/tracks` for Aria's legacy full catalog response
- `GET /api/tracks?offset=0&limit=100` for paged tracks
- `GET /api/tracks?q=nirvana&offset=0&limit=100` for paged track search
- `GET /api/search?q=nirvana` for combined track and album search
- `GET /api/albums?offset=0&limit=100` for paged album summaries
- `GET /api/albums?q=nirvana` for album search
- `GET /api/albums/<album-id>/tracks?offset=0&limit=100` for album tracks sorted by metadata track number
- `POST /api/downloads` to start a server-side YouTube playlist/album download
- `GET /api/downloads` for the active download plus recent jobs
- `GET /api/downloads/<download-id>` for progress, status, and output tail
- `GET /api/stream/<filename>` for MP3/audio streaming with byte ranges
- `GET /api/artwork/<filename>` for embedded album artwork extracted from metadata

The catalog cache stores track metadata, album IDs, artwork availability, file
sizes, and modification times. On startup, and then at most once every 10
seconds while serving catalog requests, the server only re-reads metadata for
new or changed files. Adding thousands of songs does not require probing every
file for every app launch. Delete `.aria_catalog_index.json` to force a clean
rebuild.

Downloads reuse `scripts/download.py`, the same script previously run from the
terminal. Send JSON shaped like:

```json
{
  "link": "https://www.youtube.com/playlist?list=...",
  "album": "Album name",
  "albumArtist": "Album artist",
  "year": "2026"
}
```

Only one download runs at a time. Progress is approximate while `yt-dlp` runs,
then the server refreshes the cached catalog so the apps can load the new songs.

Aria reads track numbers and album artwork through `ffprobe`/`ffmpeg`, so install FFmpeg on Fedora if it is missing:

```sh
sudo dnf install ffmpeg
```

If Fedora's firewall blocks the phone or simulator, open the dev port:

```sh
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload
```

Then check it from another machine on the same network:

```sh
curl http://192.168.0.16:8000/api/tracks
```

Or over Tailscale:

```sh
curl http://100.93.250.104:8000/api/catalog
```
