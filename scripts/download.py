#!/usr/bin/env python3
import re
import shutil
import subprocess
import sys
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

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt
from rich.table import Table


console = Console()

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


def shorten(text: str, max_len: int = 100) -> str:
    text = text.replace("\r", "").replace("\n", "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def check_requirements():
    missing = []

    if shutil.which("yt-dlp") is None:
        missing.append("yt-dlp")

    if shutil.which("node") is None:
        missing.append("node")

    if missing:
        console.print(
            Panel(
                "\n".join(f"[red]Missing:[/red] {item}" for item in missing),
                title="Requirements problem",
                border_style="red",
            )
        )

        console.print("\nInstall yt-dlp with:")
        console.print("[bold]python3 -m pip install yt-dlp[/bold]")

        console.print("\nInstall node on Fedora with:")
        console.print("[bold]sudo dnf install nodejs[/bold]")

        sys.exit(1)


def format_ytdlp_line(line: str) -> str:
    safe = escape(line)

    if "ERROR:" in line or "Traceback" in line:
        return f"[bold red]✖ {safe}[/bold red]"

    if line.startswith("[download] Destination:"):
        return f"[bold green]⬇ {safe}[/bold green]"

    if line.startswith("[download]"):
        return f"[blue]▸ {safe}[/blue]"

    if line.startswith("[ExtractAudio]"):
        return f"[magenta]🎧 {safe}[/magenta]"

    if line.startswith("[Metadata]"):
        return f"[yellow]🏷 {safe}[/yellow]"

    if line.startswith("[EmbedThumbnail]"):
        return f"[yellow]🖼 {safe}[/yellow]"

    if line.startswith("[youtube]") or line.startswith("[generic]"):
        return f"[cyan]🌐 {safe}[/cyan]"

    if "Deleting original file" in line:
        return f"[dim]🧹 {safe}[/dim]"

    return f"[dim]{safe}[/dim]"


def detect_phase(line: str) -> str:
    if line.startswith("[download] Destination:"):
        return "new file started"
    if line.startswith("[download]"):
        return "downloading"
    if line.startswith("[ExtractAudio]"):
        return "converting audio"
    if line.startswith("[Metadata]"):
        return "writing metadata"
    if line.startswith("[EmbedThumbnail]"):
        return "embedding cover"
    if line.startswith("[youtube]"):
        return "reading YouTube data"
    if "Deleting original file" in line:
        return "cleaning temp files"
    return "working"


def run_download(command: list[str]) -> bool:
    output_tail: list[str] = []

    stats = {
        "files_started": 0,
        "audio_converted": 0,
        "metadata": 0,
        "covers": 0,
    }

    console.rule("[bold cyan]yt-dlp output[/bold cyan]")

    with Progress(
        SpinnerColumn("dots"),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting yt-dlp...", total=None)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert process.stdout is not None

        for raw_line in process.stdout:
            line = raw_line.strip()

            if not line:
                continue

            output_tail.append(line)
            output_tail = output_tail[-25:]

            if line.startswith("[download] Destination:"):
                stats["files_started"] += 1
            elif line.startswith("[ExtractAudio]"):
                stats["audio_converted"] += 1
            elif line.startswith("[Metadata]"):
                stats["metadata"] += 1
            elif line.startswith("[EmbedThumbnail]"):
                stats["covers"] += 1

            phase = detect_phase(line)

            progress.console.print(format_ytdlp_line(line))

            progress.update(
                task,
                description=(
                    f"{phase}  "
                    f"[dim]| files: {stats['files_started']} "
                    f"| audio: {stats['audio_converted']} "
                    f"| metadata: {stats['metadata']} "
                    f"| covers: {stats['covers']}[/dim]"
                ),
            )

        return_code = process.wait()

    console.rule("[bold cyan]yt-dlp finished[/bold cyan]")

    if return_code != 0:
        console.print(
            Panel(
                "\n".join(escape(line) for line in output_tail)
                or "No error output captured.",
                title="[red]Download failed[/red]",
                border_style="red",
            )
        )
        return False

    console.print(
        Panel.fit(
            f"[bold green]yt-dlp finished successfully[/bold green]\n\n"
            f"[white]Files started:[/white] {stats['files_started']}\n"
            f"[white]Audio conversions:[/white] {stats['audio_converted']}\n"
            f"[white]Metadata lines:[/white] {stats['metadata']}\n"
            f"[white]Cover lines:[/white] {stats['covers']}",
            border_style="green",
        )
    )

    return True


def apply_album_metadata(
    mp3_files: list[Path],
    album: str,
    album_artist: str,
    year: str,
):
    total_tracks = len(mp3_files)
    cover_data, cover_mime = read_first_cover(mp3_files)

    console.rule("[bold cyan]Applying album metadata[/bold cyan]")

    with Progress(
        SpinnerColumn("dots"),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Tagging MP3 files", total=len(mp3_files))

        for file in mp3_files:
            track_no = get_track_number(file)
            title = get_title_from_filename(file)

            progress.console.print(
                f"[green]🏷 Tagging:[/green] [white]{escape(file.name)}[/white]"
            )

            try:
                tags = ID3(file)
            except error:
                tags = ID3()

            # Remove old album art so every new file gets the same one
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

            progress.update(
                task,
                advance=1,
                description=f"Tagged {escape(shorten(file.name, 50))}",
            )


def show_file_table(files: list[Path]):
    table = Table(
        title="New MP3 files detected",
        box=box.ROUNDED,
        show_lines=False,
        title_style="bold cyan",
    )

    table.add_column("#", justify="right", style="dim")
    table.add_column("Filename", style="green")
    table.add_column("Title", style="white")
    table.add_column("Track", justify="right", style="magenta")

    for index, file in enumerate(files, start=1):
        track_no = get_track_number(file)
        title = get_title_from_filename(file)

        table.add_row(
            str(index),
            file.name,
            title,
            str(track_no) if track_no else "-",
        )

    console.print(table)


def main():
    console.clear()

    console.print(
        Panel.fit(
            "[bold cyan]♪ ARIA Album Downloader[/bold cyan]\n"
            "[dim]Downloads playlist/album MP3s and tags only newly downloaded files.[/dim]",
            border_style="cyan",
        )
    )

    check_requirements()

    link = Prompt.ask("\n[bold cyan]Paste playlist / album link[/bold cyan]").strip()
    album = Prompt.ask("[bold cyan]Album name[/bold cyan]").strip()
    album_artist = Prompt.ask("[bold cyan]Album artist[/bold cyan]").strip()
    year = Prompt.ask("[bold cyan]Year[/bold cyan]").strip()

    if not link:
        console.print("[red]No link provided.[/red]")
        sys.exit(1)

    before = set(SONGS_DIR.glob("*.mp3"))

    command = [
        "yt-dlp",

        # Helps avoid YouTube 403 errors
        "--js-runtimes",
        "node",

        # Do not overwrite already downloaded files
        "--no-overwrites",

        # Makes yt-dlp print progress as real lines instead of rewriting one line
        "--newline",

        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--embed-thumbnail",
        "--add-metadata",
        "--yes-playlist",

        # Keep files directly inside aria-server/songs
        # Playlist position becomes track number
        "-o",
        str(SONGS_DIR / "%(playlist_index)02d - %(title)s.%(ext)s"),

        link,
    ]

    console.print(
        Panel(
            f"[bold]Album:[/bold] {escape(album)}\n"
            f"[bold]Artist:[/bold] {escape(album_artist)}\n"
            f"[bold]Year:[/bold] {escape(year or '-')}\n"
            f"[bold]Output:[/bold] {escape(str(SONGS_DIR))}\n"
            f"[bold]Safe mode:[/bold] [green]on[/green] — old MP3s will not be tagged",
            title="Download settings",
            border_style="blue",
        )
    )

    success = run_download(command)

    if not success:
        sys.exit(1)

    after = set(SONGS_DIR.glob("*.mp3"))
    new_files = sorted(after - before)

    if not new_files:
        console.print(
            Panel(
                "[yellow]No new MP3 files found.[/yellow]\n"
                "[dim]Refusing to tag old files in songs folder.[/dim]",
                title="Nothing to tag",
                border_style="yellow",
            )
        )
        sys.exit(1)

    show_file_table(new_files)

    apply_album_metadata(new_files, album, album_artist, year)

    console.print(
        Panel.fit(
            f"[bold green]Done.[/bold green]\n"
            f"[white]{len(new_files)} new file(s) downloaded and tagged.[/white]\n\n"
            f"[dim]Saved to:[/dim] {escape(str(SONGS_DIR))}",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()