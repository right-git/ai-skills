#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
}
SAFE_HEADER_NAMES = (
    "user-agent",
    "referer",
    "origin",
    "accept",
    "accept-language",
)


class CaptureError(RuntimeError):
    pass


class MediaCollector:
    def __init__(self, page_url: str):
        self.page_url = page_url
        self.events: list[dict[str, object]] = []
        self.candidates: list[dict[str, object]] = []

    @property
    def mp4_ready(self) -> bool:
        return any(item.get("kind") == "mp4" for item in self.candidates)

    def observe(self, response: object) -> None:
        try:
            response_headers = response.all_headers()
            request = response.request
            request_headers = request.all_headers()
            event = {
                "url": response.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "status": response.status,
                "content_type": response_headers.get("content-type", ""),
            }
        except Exception as exc:  # noqa: BLE001 - browser callback must not escape
            self.events.append({"error": str(exc)})
            return
        self.events.append(event)
        kind = classify_media(str(event["url"]), str(event["content_type"]))
        if kind and 200 <= int(event["status"]) < 400:
            self.candidates.append(
                {
                    **event,
                    "kind": kind,
                    "headers": safe_request_headers(request_headers, self.page_url),
                }
            )


def classify_media(url: str, content_type: str = "") -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    path = parsed.path.lower()
    mime = content_type.split(";", 1)[0].strip().lower()
    if path.endswith(".mp4") or mime == "video/mp4":
        return "mp4"
    if path.endswith(".m3u8") or mime in HLS_CONTENT_TYPES:
        return "hls"
    return None


def safe_request_headers(
    headers: dict[str, str], page_url: str
) -> dict[str, str]:
    canonical_names = {
        "user-agent": "User-Agent",
        "referer": "Referer",
        "origin": "Origin",
        "accept": "Accept",
        "accept-language": "Accept-Language",
    }
    lowered = {key.lower(): value for key, value in headers.items()}
    result = {
        canonical_names[name]: lowered[name]
        for name in SAFE_HEADER_NAMES
        if lowered.get(name)
    }
    result.setdefault("Referer", page_url)
    return result


def choose_candidate(candidates: list[dict[str, object]]) -> dict[str, object]:
    unique: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        url = candidate.get("url")
        if isinstance(url, str) and url not in unique:
            unique[url] = candidate
    for kind in ("mp4", "hls"):
        for candidate in unique.values():
            if candidate.get("kind") == kind:
                return candidate
    raise CaptureError("no usable MP4 or HLS request was captured")


def validate_page_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "clip.cafe"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("page URL must be a public HTTPS clip.cafe URL")
    return value


def prepare_destination(destination: Path, *, force: bool) -> Path:
    destination = Path(destination)
    if destination.exists() and not force:
        raise FileExistsError(
            f"destination already exists: {destination}; use --force to replace it"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def format_ffmpeg_headers(headers: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def capture_requests(
    page_url: str,
    *,
    timeout: float,
    click_selector: str,
) -> MediaCollector:
    page_url = validate_page_url(page_url)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureError(
            "Playwright is required; install the skill requirements and run "
            "`playwright install chromium`"
        ) from exc

    collector = MediaCollector(page_url)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.on("response", collector.observe)
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            target = page.locator(click_selector).first
            if target.count():
                target.click(timeout=5_000, force=True)

            deadline = time.monotonic() + timeout
            hls_seen_at: float | None = None
            while time.monotonic() < deadline:
                if collector.mp4_ready:
                    break
                if collector.candidates:
                    hls_seen_at = hls_seen_at or time.monotonic()
                    if time.monotonic() - hls_seen_at >= 1.0:
                        break
                page.wait_for_timeout(100)
            browser.close()
    except PlaywrightError as exc:
        raise CaptureError(f"browser capture failed: {exc}") from exc
    if not collector.candidates:
        raise CaptureError(
            "no public MP4 or HLS response was observed while the player ran"
        )
    return collector


def _download_mp4(
    url: str, headers: dict[str, str], destination: Path
) -> None:
    with httpx.stream(
        "GET",
        url,
        headers=headers,
        follow_redirects=True,
        timeout=60,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if not (
            content_type.startswith("video/")
            or "application/octet-stream" in content_type
        ):
            raise CaptureError(
                f"media request returned non-video content: {content_type or 'missing'}"
            )
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                if chunk:
                    output.write(chunk)


def _download_hls(
    url: str, headers: dict[str, str], destination: Path
) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-headers",
                format_ffmpeg_headers(headers),
                "-i",
                url,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(destination),
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        raise CaptureError("ffmpeg is required to download HLS media") from exc
    except subprocess.CalledProcessError as exc:
        raise CaptureError(f"ffmpeg failed with exit code {exc.returncode}") from exc


def capture_and_download(
    page_url: str,
    destination: Path,
    *,
    force: bool,
    timeout: float,
    click_selector: str = "#playerCont, video",
    network_log: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    destination = prepare_destination(destination, force=force)
    collector = capture_requests(
        page_url,
        timeout=timeout,
        click_selector=click_selector,
    )
    selected = choose_candidate(collector.candidates)
    if network_log:
        network_log.parent.mkdir(parents=True, exist_ok=True)
        network_log.write_text(
            json.dumps(
                {"events": collector.events, "candidates": collector.candidates},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part.mp4",
            delete=False,
        ) as temp_file:
            temporary = Path(temp_file.name)
        headers = selected.get("headers")
        if not isinstance(headers, dict):
            raise CaptureError("captured media request has no reusable headers")
        if selected.get("kind") == "mp4":
            _download_mp4(str(selected["url"]), headers, temporary)
        else:
            _download_hls(str(selected["url"]), headers, temporary)
        if not temporary.stat().st_size:
            raise CaptureError("downloaded media file is empty")
        if force:
            temporary.replace(destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        return destination, selected
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture public Clip.Cafe player media from browser traffic"
    )
    parser.add_argument("page_url", help="Public Clip.Cafe clip page URL")
    parser.add_argument("output", type=Path, help="Destination MP4 path")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--click-selector", default="#playerCont, video")
    parser.add_argument("--network-log", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        saved, selected = capture_and_download(
            args.page_url,
            args.output,
            force=args.force,
            timeout=args.timeout,
            click_selector=args.click_selector,
            network_log=args.network_log,
        )
    except (CaptureError, FileExistsError, ValueError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {"saved": str(saved), "media": selected},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"saved: {saved}")
        print(f"media: {selected['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
