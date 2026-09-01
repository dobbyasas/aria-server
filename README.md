# Aria Song Server

Current Aria version: `1.15.0`

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
- `GET /api/tracks/<track-id>/lyrics` for synchronized or plain lyrics
- `POST /api/tracks/<track-id>/artwork/refresh` to replace an album's embedded cover from a validated YouTube image
- `GET /api/search?q=nirvana` for combined track and album search
- `GET /api/albums?offset=0&limit=100` for paged album summaries
- `GET /api/albums?q=nirvana` for album search
- `GET /api/albums/<album-id>/tracks?offset=0&limit=100` for album tracks sorted by metadata track number
- `DELETE /api/albums/<album-id>` to delete every file in one album
- `DELETE /api/tracks/<track-id>/album` to delete the album containing a selected track
- `GET /api/playlists` for shared playlists on every Aria device
- `PUT /api/playlists/<playlist-id>` to create or update a shared playlist
- `DELETE /api/playlists/<playlist-id>` to remove a shared playlist
- `POST /api/downloads` to start a server-side YouTube album, song, or playlist download
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

Shared playlists are stored atomically in `songs/.aria_playlists.json`. The
server preserves their names, ordered track IDs, compressed custom covers, and
edit revisions so stale updates from another device cannot overwrite newer edits.

Lyrics are resolved from a matching `.lrc`, `.lyrics`, or `.txt` sidecar first,
then from embedded audio tags, and finally from LRCLIB. Online results are
cached under `~/.cache/aria-song-server/lyrics`; unsuccessful matches are
retried after one day.

Downloads reuse `scripts/download.py`, the same script previously run from the
terminal. Send JSON shaped like:

```json
{
  "link": "https://www.youtube.com/playlist?list=...",
  "album": "Album name",
  "albumArtist": "Album artist",
  "year": "2026",
  "kind": "album"
}
```

`kind` may be `album`, `song`, or `playlist`. Album downloads require the album
fields and retain the existing album-wide retagging. Song and playlist downloads
preserve source metadata and are recorded in `.aria_standalone_tracks.json`, so
they stay visible in the song library without creating partial album cards.
Before downloading a song or playlist, the server inspects its YouTube entries
and matches them against the catalog by YouTube video ID, then normalized title
and artist. Only missing playlist positions are downloaded. A stable shared Aria
playlist is created or updated with both reused and new tracks in source order.

Only one download runs at a time. Progress is approximate while `yt-dlp` runs,
then the server refreshes the cached catalog so the apps can load the new songs.

## Downloader Auto-update

YouTube changes frequently, so Aria maintains a separate, validated downloader
installation. The updater installs the latest yt-dlp nightly release and its
default dependencies (including EJS) in an isolated environment. It then makes
a real 10 KiB YouTube test transfer. The new version becomes active atomically
only when that test succeeds; the previous validated version is retained for
rollback.

Install or update the daily timer:

```sh
sudo cp systemd/aria-downloader-update.service /etc/systemd/system/
sudo cp systemd/aria-downloader-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aria-downloader-update.timer
sudo systemctl start aria-downloader-update.service
```

Check the updater and its logs:

```sh
systemctl status aria-downloader-update.timer
systemctl status aria-downloader-update.service
journalctl -u aria-downloader-update.service -n 100 --no-pager
```

The timer runs every day around 04:15, with a randomized delay of up to 45
minutes. `Persistent=true` makes systemd run a missed update when the laptop
next starts. The apps and song server do not need to restart after a successful
update because every download starts the currently validated yt-dlp executable.
If a download still encounters a recognizable YouTube 403 or challenge error,
Aria immediately runs the same safe updater and retries that download once.

Aria reads track numbers and album artwork through `ffprobe`/`ffmpeg`, so install FFmpeg on Fedora if it is missing:

```sh
sudo dnf install ffmpeg
```

If Fedora's firewall blocks the phone or simulator, open the dev port:

```sh
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload
```

## Network Watchdog

The Fedora laptop also runs a watchdog for short Wi-Fi drops. It waits for the
network to return, restarts `tailscaled` if `tailscale0` loses its `100.x`
address, and restarts the Aria song server if `/api/catalog` stops responding.

Install or update the units from this repo:

```sh
sudo cp systemd/aria-song-server.service /etc/systemd/system/
sudo cp systemd/aria-network-watchdog.service /etc/systemd/system/
sudo cp systemd/aria-network-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aria-song-server.service
sudo systemctl enable --now aria-network-watchdog.timer
```

Check status:

```sh
systemctl status aria-song-server.service
systemctl status aria-network-watchdog.timer
sudo systemctl start aria-network-watchdog.service
journalctl -u aria-network-watchdog.service -n 80 --no-pager
```

Then check it from another machine on the same network:

```sh
curl http://192.168.0.16:8000/api/tracks
```

Or over Tailscale:

```sh
curl http://100.93.250.104:8000/api/catalog
```
