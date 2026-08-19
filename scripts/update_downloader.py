#!/usr/bin/env python3
"""Safely update and validate Aria's managed yt-dlp installation."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_INSTALL_ROOT = Path.home() / ".local" / "share" / "aria-downloader"
DEFAULT_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
REQUIRED_COMMANDS = ("node", "ffmpeg", "ffprobe")


class UpdateError(RuntimeError):
    pass


def log(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


def require_commands() -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []

    for command in REQUIRED_COMMANDS:
        path = shutil.which(command)
        if path:
            resolved[command] = path
        else:
            missing.append(command)

    if missing:
        raise UpdateError(f"Missing required command(s): {', '.join(missing)}")

    return resolved


def command_output(command: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"Command failed: {' '.join(command)}: {error}") from error

    return result.stdout.strip()


def install_candidate(candidate: Path, channel: str) -> Path:
    log(f"Creating isolated candidate in {candidate}.")
    subprocess.run(
        [sys.executable, "-m", "venv", str(candidate)],
        check=True,
        timeout=120,
    )

    python = candidate / "bin" / "python"
    install_command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
    ]
    if channel == "nightly":
        install_command.append("--pre")
    install_command.append("yt-dlp[default]")

    log(f"Installing the latest yt-dlp {channel} build and default dependencies.")
    subprocess.run(install_command, check=True, timeout=300)

    executable = candidate / "bin" / "yt-dlp"
    if not executable.is_file():
        raise UpdateError("The candidate installation did not create yt-dlp.")

    return executable


def validate_downloader(executable: Path, node_path: str, test_url: str) -> str:
    version = command_output([str(executable), "--version"])
    log(f"Validating yt-dlp {version} with a real 10 KiB YouTube transfer.")

    with tempfile.TemporaryDirectory(prefix="aria-ytdlp-health-") as temp_dir:
        output_template = str(Path(temp_dir) / "healthcheck.%(ext)s")
        command = [
            str(executable),
            "--ignore-config",
            "--no-playlist",
            "--test",
            "--no-part",
            "--js-runtimes",
            f"node:{node_path}",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "youtube:player_client=web_embedded",
            "--format",
            "bestaudio/best",
            "--output",
            output_template,
            test_url,
        ]

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise UpdateError(f"Downloader health check could not run: {error}") from error

        files = [path for path in Path(temp_dir).iterdir() if path.is_file()]
        if result.returncode != 0 or not any(path.stat().st_size > 0 for path in files):
            output_tail = "\n".join(result.stdout.splitlines()[-20:])
            raise UpdateError(
                f"Downloader health check failed with status {result.returncode}:\n{output_tail}"
            )

    log(f"yt-dlp {version} passed the transfer health check.")
    return version


def safe_version(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


def replace_symlink(link: Path, target: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise UpdateError(f"Refusing to replace non-symlink path: {link}")

    temporary_link = link.with_name(f".{link.name}.{os.getpid()}")
    temporary_link.unlink(missing_ok=True)
    relative_target = os.path.relpath(target.resolve(), link.parent.resolve())
    temporary_link.symlink_to(relative_target)
    os.replace(temporary_link, link)


def linked_version(link: Path, versions_dir: Path) -> Path | None:
    if link.exists() and not link.is_symlink():
        raise UpdateError(f"Refusing to replace non-symlink path: {link}")

    if not link.is_symlink():
        return None

    target = link.resolve(strict=False)
    try:
        target.relative_to(versions_dir.resolve())
    except ValueError:
        raise UpdateError(f"Refusing to use link outside managed versions: {link} -> {target}")

    return target if target.is_dir() else None


def clean_old_versions(versions_dir: Path, keep: int, protected: set[Path]) -> None:
    versions = sorted(
        (path for path in versions_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = 0

    for path in versions:
        resolved = path.resolve()
        if resolved in protected or retained < keep:
            retained += 1
            continue

        log(f"Removing old validated version {path.name}.")
        shutil.rmtree(path)


def activate_candidate(candidate: Path, install_root: Path, version: str, keep: int) -> Path:
    candidate = candidate.resolve()
    install_root = install_root.resolve()
    versions_dir = install_root / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    current_link = install_root / "current"
    previous_link = install_root / "previous"
    old_current = linked_version(current_link, versions_dir)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = versions_dir / f"{safe_version(version)}-{stamp}-{os.getpid()}"
    candidate.rename(target)

    if old_current:
        replace_symlink(previous_link, old_current)
    replace_symlink(current_link, target)

    protected = {target.resolve()}
    previous = linked_version(previous_link, versions_dir)
    if previous:
        protected.add(previous.resolve())
    clean_old_versions(versions_dir, max(keep, 2), protected)

    log(f"Activated yt-dlp {version} at {current_link}.")
    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install, verify, and atomically activate Aria's yt-dlp stack."
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        default=DEFAULT_INSTALL_ROOT,
        help=f"Managed installation directory (default: {DEFAULT_INSTALL_ROOT})",
    )
    parser.add_argument(
        "--channel",
        choices=("nightly", "stable"),
        default="nightly",
        help="yt-dlp release channel (default: nightly)",
    )
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--keep", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    install_root = args.install_root.expanduser().resolve()
    install_root.mkdir(parents=True, exist_ok=True)
    commands = require_commands()
    candidate = Path(tempfile.mkdtemp(prefix=".candidate-", dir=install_root))

    try:
        executable = install_candidate(candidate, args.channel)
        version = validate_downloader(executable, commands["node"], args.test_url)
        activate_candidate(candidate, install_root, version, args.keep)
    except (OSError, subprocess.SubprocessError, UpdateError) as error:
        log(f"Update failed; the current validated downloader was not changed: {error}")
        shutil.rmtree(candidate, ignore_errors=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
