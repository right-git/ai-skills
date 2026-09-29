#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://clip.cafe/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
)

RESULT_FIELDS = (
    "clip_id",
    "slug",
    "movie_slug",
    "movie_title",
    "movie_year",
    "season",
    "episode",
    "displayed_quote",
    "matched_line",
    "exact",
    "match_location",
    "match_start_sec",
    "match_end_sec",
    "movie_timestamp",
    "source_match_start",
    "source_match_end",
    "duration_sec",
    "resolution",
    "quality",
    "audio_languages",
    "subtitle_languages",
    "page_url",
    "thumbnail_url",
    "mp4_url",
    "hls_url",
    "enrichment_errors",
)


class SearchError(RuntimeError):
    pass


class RateLimitError(SearchError):
    pass


class DownloadError(RuntimeError):
    pass


def empty_result() -> dict[str, object]:
    result = {field: None for field in RESULT_FIELDS}
    result["audio_languages"] = []
    result["subtitle_languages"] = []
    result["enrichment_errors"] = []
    return result


def parse_api_payload(payload: object) -> tuple[int, list[dict[str, object]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise TypeError("Clip.Cafe JSON response has no results list")
    results = []
    for raw in payload["results"]:
        if not isinstance(raw, dict):
            raise TypeError("Clip.Cafe JSON result is not an object")
        result = empty_result()
        result.update(
            {
                "clip_id": raw.get("clipID"),
                "slug": raw.get("slug"),
                "movie_slug": raw.get("movie_slug"),
                "movie_title": raw.get("movie_title"),
                "movie_year": raw.get("movie_year"),
                "season": raw.get("season") or None,
                "episode": raw.get("episode") or None,
                "displayed_quote": raw.get("title"),
                "matched_line": raw.get("lineText"),
                "match_location": raw.get("matchIn"),
                "duration_sec": raw.get("duration"),
                "resolution": raw.get("resolution"),
                "quality": raw.get("quality"),
                "audio_languages": raw.get("audioLangs") or [],
                "subtitle_languages": raw.get("subLangs") or [],
                "mp4_url": raw.get("mp4Url"),
                "hls_url": raw.get("hlsUrl"),
            }
        )
        if result["movie_slug"] and result["slug"]:
            result["page_url"] = urljoin(
                BASE_URL, f"{result['movie_slug']}/{result['slug']}/"
            )
        results.append(result)
    total = payload.get("total", len(results))
    return int(total), results


def parse_search_html(html: str) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for card in soup.select(".searchResultClip"):
        result = empty_result()
        clip_link = card.select_one("a.smallClipContainer")
        picture = card.select_one("picture[data-slug]")
        movie = card.select_one(".clipMovie")
        movie_link = movie.select_one("a") if movie else None
        transcript = card.select_one(".clipTrans p")
        image = card.select_one("picture img")

        if clip_link and clip_link.get("href"):
            result["page_url"] = urljoin(BASE_URL, clip_link["href"])
        if picture:
            result["slug"] = picture.get("data-slug")
        if movie_link:
            result["movie_title"] = movie_link.get_text(" ", strip=True)
            result["movie_slug"] = movie_link.get("href", "").strip("/") or None
        if movie:
            year = re.search(r"\b(18|19|20)\d{2}\b", movie.get_text(" ", strip=True))
            if year:
                result["movie_year"] = int(year.group(0))
        if transcript:
            quote = transcript.get_text(" ", strip=True)
            result["displayed_quote"] = re.sub(
                r"^Transcript:\s*", "", quote, flags=re.IGNORECASE
            )
        if image:
            thumbnail = image.get("data-src") or image.get("src")
            if thumbnail and not thumbnail.startswith("data:"):
                result["thumbnail_url"] = urljoin(BASE_URL, thumbnail)
        if result["page_url"] or result["slug"]:
            results.append(result)
    return results


def parse_detail_html(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    details: dict[str, object] = {
        "clip_id": None,
        "movie_timestamp": None,
        "duration_sec": None,
        "thumbnail_url": None,
        "mp4_url": None,
    }
    player = soup.select_one("#playerCont[onclick]")
    if player:
        clip_id = re.search(r"startplayer\(['\"]?(\d+)", player.get("onclick", ""))
        if clip_id:
            details["clip_id"] = int(clip_id.group(1))

    source = soup.select_one("video source[type='video/mp4'][src]")
    if source:
        details["mp4_url"] = urljoin(BASE_URL, source["src"])
    image = soup.select_one("#playerCont picture img")
    if image:
        thumbnail = image.get("data-src") or image.get("src")
        if thumbnail and not thumbnail.startswith("data:"):
            details["thumbnail_url"] = urljoin(BASE_URL, thumbnail)

    for item in soup.select(".clip-meta-item"):
        label = item.select_one(".clip-meta-label")
        value = item.select_one(".clip-meta-value")
        if not label or not value:
            continue
        key = label.get_text(" ", strip=True).casefold()
        text = value.get_text(" ", strip=True)
        if key == "timestamp in movie":
            details["movie_timestamp"] = text or None
        elif key == "duration":
            seconds = re.search(r"\d+", text)
            if seconds:
                details["duration_sec"] = int(seconds.group(0))
    return details


def apply_anchor(result: dict[str, object], anchor: object) -> bool:
    if not isinstance(anchor, dict) or not anchor.get("ok"):
        return False
    start = anchor.get("startSec")
    end = anchor.get("endSec")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return False
    result["match_start_sec"] = start
    result["match_end_sec"] = end
    result["matched_line"] = anchor.get("lineText") or result.get("matched_line")
    result["exact"] = bool(anchor.get("exact"))
    timestamp = result.get("movie_timestamp")
    if isinstance(timestamp, str):
        result["source_match_start"] = _offset_timestamp(timestamp, float(start))
        result["source_match_end"] = _offset_timestamp(timestamp, float(end))
    return True


def _offset_timestamp(timestamp: str, offset_sec: float) -> str | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", timestamp)
    if not match:
        return None
    hours, minutes, seconds = (int(match.group(index)) for index in (1, 2, 3))
    milliseconds = int((match.group(4) or "0").ljust(3, "0"))
    total_ms = (
        ((hours * 60 + minutes) * 60 + seconds) * 1000
        + milliseconds
        + round(offset_sec * 1000)
    )
    out_hours, remainder = divmod(total_ms, 3_600_000)
    out_minutes, remainder = divmod(remainder, 60_000)
    out_seconds, out_ms = divmod(remainder, 1000)
    return f"{out_hours:02d}:{out_minutes:02d}:{out_seconds:02d}.{out_ms:03d}"


def enrich_results(
    client: object,
    query: str,
    language: str,
    results: list[dict[str, object]],
) -> None:
    for result in results:
        errors = result.setdefault("enrichment_errors", [])
        page_url = result.get("page_url")
        if page_url:
            try:
                response = client.get(page_url)
                raise_for_rate_limit(response)
                response.raise_for_status()
                for key, value in parse_detail_html(response.text).items():
                    if value is not None and result.get(key) in (None, "", []):
                        result[key] = value
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional remote enrichment
                errors.append(f"detail: {exc}")

        clip_id = result.get("clip_id")
        if clip_id:
            try:
                response = client.get(
                    BASE_URL,
                    params={
                        "phraseanchor": clip_id,
                        "q": query,
                        "lang": language,
                    },
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                raise_for_rate_limit(response)
                response.raise_for_status()
                if not apply_anchor(result, response.json()):
                    errors.append("anchor: no timed subtitle match")
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional remote enrichment
                errors.append(f"anchor: {exc}")


def raise_for_rate_limit(response: object) -> None:
    if getattr(response, "status_code", None) != 429:
        return
    retry_after = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            retry_after = payload.get("retry_after")
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is None:
        retry_after = getattr(response, "headers", {}).get("Retry-After")
    suffix = f"; retry after {retry_after} seconds" if retry_after else ""
    raise RateLimitError(f"Clip.Cafe rate limit reached{suffix}")


def api_params_to_page_params(
    params: dict[str, str | int],
) -> dict[str, str | int]:
    page_params = {
        key: value
        for key, value in params.items()
        if key not in {"phrasefind", "from", "size"}
    }
    page_params["s"] = params.get("phrasefind", "")
    page_params["ss"] = "s"
    return page_params


def search_clips(client: object, params: dict[str, str | int]) -> dict[str, object]:
    api_error: Exception | None = None
    try:
        response = client.get(
            BASE_URL,
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        raise_for_rate_limit(response)
        response.raise_for_status()
        total, results = parse_api_payload(response.json())
        return {"source": "api", "total": total, "results": results}
    except RateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001 - incompatible JSON triggers HTML fallback
        api_error = exc

    try:
        response = client.get(BASE_URL, params=api_params_to_page_params(params))
        raise_for_rate_limit(response)
        response.raise_for_status()
        results = parse_search_html(response.text)
        return {"source": "html", "total": len(results), "results": results}
    except RateLimitError:
        raise
    except Exception as page_error:
        raise SearchError(
            f"JSON search failed: {api_error}; HTML fallback failed: {page_error}"
        ) from page_error


def pick_result(results: list[dict[str, object]], pick: int) -> dict[str, object]:
    if pick < 1 or pick > len(results):
        raise ValueError(
            f"--pick {pick} is outside the available result range 1–{len(results)}"
        )
    return results[pick - 1]


def select_result(
    results: list[dict[str, object]], pick: int, slug: str | None
) -> dict[str, object]:
    if slug:
        for result in results:
            if result.get("slug") == slug:
                return result
        raise ValueError(f"--slug {slug!r} was not found in the current results")
    return pick_result(results, pick)


def results_to_enrich(
    results: list[dict[str, object]],
    *,
    downloading: bool,
    pick: int,
    slug: str | None,
) -> list[dict[str, object]]:
    if not downloading:
        return results
    return [select_result(results, pick, slug)]


def _validated_mp4_url(result: dict[str, object]) -> str:
    value = result.get("mp4_url")
    if not isinstance(value, str) or not value:
        raise DownloadError("selected result has no MP4 URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "clip.cafe"
        or not parsed.path.lower().endswith(".mp4")
    ):
        raise DownloadError(
            "selected result does not contain a valid Clip.Cafe MP4 URL"
        )
    return value


def download_clip(
    client: object,
    result: dict[str, object],
    destination: Path,
    *,
    force: bool,
) -> Path:
    destination = Path(destination)
    if destination.exists() and not force:
        raise FileExistsError(
            f"destination already exists: {destination}; use --force only to replace it"
        )
    mp4_url = _validated_mp4_url(result)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            request_headers = {
                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            }
            if result.get("page_url"):
                request_headers["Referer"] = str(result["page_url"])
            with client.stream("GET", mp4_url, headers=request_headers) as response:
                if getattr(response, "status_code", None) == 403:
                    page_url = result.get("page_url")
                    review_hint = (
                        f" Open the clip page and use Clip.Cafe's authorized "
                        f"download flow: {page_url}"
                        if page_url
                        else " Open the clip page and use Clip.Cafe's authorized "
                        "download flow."
                    )
                    raise DownloadError(
                        "Clip.Cafe denied the media download (HTTP 403). This can "
                        "mean the account is not authorized or its daily download "
                        f"limit was reached.{review_hint}"
                    )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not (
                    content_type.startswith("video/")
                    or "application/octet-stream" in content_type
                ):
                    raise DownloadError(
                        f"download returned non-video content type: {content_type or 'missing'}"
                    )
                for chunk in response.iter_bytes():
                    if chunk:
                        temp_file.write(chunk)
        if not temp_path.stat().st_size:
            raise DownloadError("download returned an empty file")
        if force:
            temp_path.replace(destination)
        else:
            os.link(temp_path, destination)
            temp_path.unlink()
        return destination
    except Exception:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        raise


def run_browser_capture(
    page_url: str,
    destination: Path,
    *,
    force: bool,
    timeout: float,
) -> Path:
    script = Path(__file__).with_name("capture-media.py")
    spec = importlib.util.spec_from_file_location("clip_cafe_capture_media", script)
    if not spec or not spec.loader:
        raise DownloadError(f"cannot load browser capture tool: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved, _selected = module.capture_and_download(
        page_url,
        destination,
        force=force,
        timeout=timeout,
    )
    return saved


def download_result(
    client: object,
    result: dict[str, object],
    destination: Path,
    *,
    force: bool,
    browser_fallback: bool,
    timeout: float,
    capture_runner=run_browser_capture,
) -> Path:
    try:
        return download_clip(client, result, destination, force=force)
    except DownloadError:
        page_url = result.get("page_url")
        if not browser_fallback or not isinstance(page_url, str) or not page_url:
            raise
        try:
            return capture_runner(
                page_url,
                Path(destination),
                force=force,
                timeout=timeout,
            )
        except (DownloadError, FileExistsError):
            raise
        except Exception as exc:  # noqa: BLE001 - normalize optional tool errors
            raise DownloadError(f"browser fallback failed: {exc}") from exc


def _shown(value: object) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def format_human(results: list[dict[str, object]]) -> str:
    if not results:
        return "No clips found."
    blocks = []
    for index, result in enumerate(results, start=1):
        title = _shown(result.get("movie_title"))
        year = _shown(result.get("movie_year"))
        heading = f"{index}. {title} ({year})"
        if result.get("season"):
            heading += f" — S{result['season']}E{_shown(result.get('episode'))}"
        start = _shown(result.get("match_start_sec"))
        end = _shown(result.get("match_end_sec"))
        blocks.append(
            "\n".join(
                (
                    heading,
                    f"   line: {_shown(result.get('matched_line') or result.get('displayed_quote'))}",
                    f"   clip time: {start}–{end} seconds",
                    f"   clip starts in source: {_shown(result.get('movie_timestamp'))}",
                    (
                        "   phrase in source: "
                        f"{_shown(result.get('source_match_start'))}–"
                        f"{_shown(result.get('source_match_end'))}"
                    ),
                    f"   page: {_shown(result.get('page_url'))}",
                    f"   mp4: {_shown(result.get('mp4_url'))}",
                )
            )
        )
    return "\n\n".join(blocks)


def _applied_filters(args: argparse.Namespace) -> dict[str, object]:
    filters: dict[str, object] = {}
    if args.exact:
        filters["exact"] = True
    for name in ("title", "actor", "character", "director", "writer", "genre"):
        value = getattr(args, name)
        if value:
            filters[name] = value
    optional = {
        "type": (args.type, "all"),
        "rating": (args.rating, "any"),
        "language": (args.language, "en"),
        "audio_language": (args.audio_language, None),
        "subtitle_language": (args.subtitle_language, None),
        "decade": (args.decade, None),
        "year_start": (args.year_start, None),
        "year_end": (args.year_end, None),
        "duration_start": (args.duration_start, None),
        "duration_end": (args.duration_end, None),
        "sort": (args.sort, "relevance"),
    }
    for name, (value, default) in optional.items():
        if value != default and value is not None:
            filters[name] = value
    return filters


def build_output_document(
    args: argparse.Namespace, search: dict[str, object]
) -> dict[str, object]:
    return {
        "query": args.query,
        "filters": _applied_filters(args),
        "source": search["source"],
        "total": search["total"],
        "results": search["results"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        params = build_search_params(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=args.timeout,
        ) as client:
            found = search_clips(client, params)
            results = found["results"]
            assert isinstance(results, list)
            enrichment_targets = results_to_enrich(
                results,
                downloading=args.download is not None,
                pick=args.pick,
                slug=args.slug,
            )
            enrich_results(
                client,
                str(params["phrasefind"]),
                args.language,
                enrichment_targets,
            )
            if args.download:
                selected = enrichment_targets[0]
                saved = download_result(
                    client,
                    selected,
                    args.download,
                    force=args.force,
                    browser_fallback=args.browser_fallback,
                    timeout=args.timeout,
                )
                print(f"saved: {saved}", file=sys.stderr)
    except (
        SearchError,
        DownloadError,
        FileExistsError,
        ValueError,
        httpx.HTTPError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        document = build_output_document(args, found)
        if args.json:
            print(json.dumps(document, ensure_ascii=False, indent=2))
        else:
            print(format_human(results))
    return 0


def parse_decade(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2}|\d{4})s?", value.strip().lower())
    if not match:
        raise ValueError(f"invalid decade: {value!r}")
    year = int(match.group(1))
    if year < 100:
        year += 1900
    if year % 10:
        raise ValueError(f"decade must begin on a ten-year boundary: {value!r}")
    return year, year + 9


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find precisely timed movie and TV clips on Clip.Cafe"
    )
    parser.add_argument("query", help="Spoken quote or scene phrase")
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--title")
    parser.add_argument("--actor")
    parser.add_argument("--character")
    parser.add_argument("--director")
    parser.add_argument("--writer")
    parser.add_argument("--type", choices=("all", "movie", "tv"), default="all")
    parser.add_argument(
        "--rating", choices=("any", "family", "teen", "mature"), default="any"
    )
    parser.add_argument("--genre")
    parser.add_argument("--language", default="en")
    parser.add_argument("--audio-language")
    parser.add_argument("--subtitle-language")
    parser.add_argument("--decade")
    parser.add_argument("--year-start", type=int)
    parser.add_argument("--year-end", type=int)
    parser.add_argument("--duration-start", type=int)
    parser.add_argument("--duration-end", type=int)
    parser.add_argument(
        "--sort",
        choices=("relevance", "views", "newest", "shortest", "longest"),
        default="relevance",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the result list after a download",
    )
    parser.add_argument("--download", type=Path)
    parser.add_argument("--pick", type=int, default=1)
    parser.add_argument(
        "--slug", help="Stable result slug to download instead of --pick"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--browser-fallback",
        action="store_true",
        help="capture public player traffic if direct MP4 download fails",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


def _validate_range(name: str, start: int | None, end: int | None) -> None:
    if start is not None and start < 0:
        raise ValueError(f"{name} start cannot be negative")
    if end is not None and end < 0:
        raise ValueError(f"{name} end cannot be negative")
    if start is not None and end is not None and start > end:
        raise ValueError(f"{name} start cannot be greater than its end")


def build_search_params(args: argparse.Namespace) -> dict[str, str | int]:
    query = args.query.strip()
    if not query:
        raise ValueError("query cannot be empty")
    if not 1 <= args.limit <= 90:
        raise ValueError("--limit must be between 1 and 90")
    if args.pick < 1:
        raise ValueError("--pick must be at least 1")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.decade and (args.year_start is not None or args.year_end is not None):
        raise ValueError("--decade cannot be combined with explicit year bounds")

    year_start, year_end = args.year_start, args.year_end
    if args.decade:
        year_start, year_end = parse_decade(args.decade)
    _validate_range("year", year_start, year_end)
    _validate_range("duration", args.duration_start, args.duration_end)

    phrase = f'"{query}"' if args.exact else query
    params: dict[str, str | int] = {
        "phrasefind": phrase,
        "lang": args.language,
        "from": 0,
        "size": args.limit,
    }
    direct = {
        "title": args.title,
        "actor": args.actor,
        "character": args.character,
        "director": args.director,
        "writer": args.writer,
        "genre": args.genre,
        "audio": args.audio_language,
        "subs": args.subtitle_language,
        "yearStart": year_start,
        "yearEnd": year_end,
        "durationStart": args.duration_start,
        "durationEnd": args.duration_end,
    }
    params.update(
        {key: value for key, value in direct.items() if value not in (None, "")}
    )
    if args.type != "all":
        params["type"] = args.type
    if args.rating != "any":
        params["rated"] = args.rating
    if args.sort != "relevance":
        params["sort"] = args.sort
    return params


if __name__ == "__main__":
    raise SystemExit(main())
