#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from http.client import HTTPConnection


DEFAULT_PORT = 8000
DEFAULT_WAIT_SECONDS = 900
NETWORK_PROBE = "1.1.1.1"
TAILSCALE_INTERFACE = "tailscale0"
TAILSCALE_IPV4_PREFIX = "100."
ARIA_SERVICE = "aria-song-server.service"
TAILSCALE_SERVICE = "tailscaled.service"


def log(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


def run(command: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
    )


def command_ok(command: list[str], timeout: int = 8) -> bool:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False

    return result.returncode == 0


def wait_for_network(max_wait_seconds: int) -> bool:
    deadline = time.monotonic() + max_wait_seconds

    while True:
        if command_ok(["ping", "-c", "1", "-W", "2", NETWORK_PROBE], timeout=5):
            return True

        if time.monotonic() >= deadline:
            return False

        log("Network is not reachable yet; waiting before touching Tailscale.")
        time.sleep(15)


def tailscale_ipv4() -> str | None:
    result = run(["ip", "-j", "-4", "addr", "show", "dev", TAILSCALE_INTERFACE])
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    for interface in payload:
        for address in interface.get("addr_info", []):
            local = str(address.get("local") or "")
            if local.startswith(TAILSCALE_IPV4_PREFIX):
                return local

    return None


def service_is_active(name: str) -> bool:
    return command_ok(["systemctl", "is-active", "--quiet", name], timeout=8)


def restart_service(name: str) -> None:
    log(f"Restarting {name}.")
    result = run(["systemctl", "restart", name])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Could not restart {name}: {detail}")


def wait_for_tailscale_ip(max_wait_seconds: int = 90) -> bool:
    deadline = time.monotonic() + max_wait_seconds

    while time.monotonic() < deadline:
        if tailscale_ipv4():
            return True

        time.sleep(3)

    return False


def tailscale_status_is_usable() -> bool:
    result = run(["tailscale", "status", "--self"])
    return result.returncode == 0 and "tofios" in result.stdout


def ensure_tailscale() -> str | None:
    if not service_is_active(TAILSCALE_SERVICE):
        restart_service(TAILSCALE_SERVICE)
        wait_for_tailscale_ip()

    current_ip = tailscale_ipv4()
    if current_ip and tailscale_status_is_usable():
        log(f"Tailscale is healthy at {current_ip}.")
        return current_ip

    log("Tailscale is active but not usable; restarting tailscaled.")
    restart_service(TAILSCALE_SERVICE)

    if wait_for_tailscale_ip():
        current_ip = tailscale_ipv4()
        log(f"Tailscale recovered at {current_ip}.")
        return current_ip

    log("Tailscale did not recover an IPv4 address yet.")
    return None


def http_health(host: str, port: int) -> bool:
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", "/api/catalog")
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        connection.close()


def ensure_aria_server(port: int, tailscale_ip: str | None) -> None:
    local_ok = http_health("127.0.0.1", port)
    tailscale_ok = http_health(tailscale_ip, port) if tailscale_ip else False

    if local_ok and (tailscale_ip is None or tailscale_ok):
        log("Aria server is healthy.")
        return

    log(
        "Aria server health check failed "
        f"(local={local_ok}, tailscale={tailscale_ok}); restarting server."
    )
    restart_service(ARIA_SERVICE)
    time.sleep(3)

    local_ok = http_health("127.0.0.1", port)
    tailscale_ok = http_health(tailscale_ip, port) if tailscale_ip else False

    if local_ok and (tailscale_ip is None or tailscale_ok):
        log("Aria server recovered.")
        return

    raise RuntimeError(
        "Aria server did not recover "
        f"(local={local_ok}, tailscale={tailscale_ok})."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Heal Aria server and Tailscale after Wi-Fi drops.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-wait", type=int, default=DEFAULT_WAIT_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not wait_for_network(args.max_wait):
        log("Network did not come back before max wait elapsed.")
        return 75

    try:
        tailscale_ip = ensure_tailscale()
        ensure_aria_server(args.port, tailscale_ip)
    except Exception as error:
        log(f"Watchdog failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
