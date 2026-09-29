"""Target resolution, pre-flight validation and the publish fan-out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from models import (
    LIMITS,
    PLATFORMS,
    Account,
    Content,
    Credentials,
    Kind,
    PlanEntry,
    PublishResult,
    YOUTUBE_TITLE_CHARS,
)
from platforms import instagram, telegram, threads, x, youtube
from utils import (
    ConfigError,
    MediaHostSession,
    credentials_lock,
    load_credentials,
    probe,
    save_credentials,
)

CLIENTS = {
    "instagram": instagram.InstagramClient,
    "threads": threads.ThreadsClient,
    "x": x.XClient,
    "telegram": telegram.TelegramClient,
    "youtube": youtube.YouTubeClient,
}
REFRESHERS = {"instagram": instagram.refresh, "threads": threads.refresh}
REFRESH_WINDOW = timedelta(days=10)

UPLOAD_ROUTE = {
    "instagram": {"video": "native resumable", "photo": "s3 then public url"},
    "threads": {"video": "s3 then public url", "photo": "s3 then public url"},
    "x": {"video": "chunked v2 upload", "photo": "chunked v2 upload"},
    "telegram": {"video": "multipart", "photo": "multipart"},
    "youtube": {"video": "resumable chunked upload"},
}


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve_targets(
    creds: Credentials, name: str, platforms: Sequence[str]
) -> list[Account]:
    """Pick the accounts named by --account/--platform, or raise ConfigError."""

    owned = {a.platform: a for a in creds.accounts if a.name == name}
    if not owned:
        known = sorted({a.name for a in creds.accounts})
        raise ConfigError(f"unknown account {name!r}; configured: {', '.join(known)}")
    if list(platforms) == ["all"]:
        return [owned[p] for p in PLATFORMS if p in owned]
    targets = []
    for platform in platforms:
        if platform not in PLATFORMS:
            raise ConfigError(
                f"unknown platform {platform!r}; expected one of "
                f"{', '.join(PLATFORMS)} or 'all'"
            )
        if platform not in owned:
            raise ConfigError(
                f"account {name!r} has no {platform!r} entry; it has: "
                f"{', '.join(sorted(owned))}"
            )
        targets.append(owned[platform])
    return targets


def build_content(
    kind: Kind,
    files: Sequence[str],
    caption: str,
    overrides: dict[str, str],
    cover: str | None = None,
    title: str | None = None,
    tags: Sequence[str] = (),
    privacy: str = "public",
) -> Content:
    """Probe every media file and resolve the caption for each platform."""

    items = []
    for raw in files:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ConfigError(f"media file not found: {path}")
        items.append(probe(path))
    captions = {platform: overrides.get(platform, caption) for platform in PLATFORMS}
    cover_path = None
    if cover:
        cover_path = Path(cover).expanduser()
        if not cover_path.is_file():
            raise ConfigError(f"cover file not found: {cover_path}")
    return Content(
        kind=kind,
        captions=captions,
        items=items,
        cover=cover_path,
        title=title.strip() if title else None,
        tags=[t.strip() for t in tags if t.strip()],
        privacy=privacy,  # type: ignore[arg-type]
    )


def needs_media_host(platform: str, kind: Kind) -> bool:
    """True when the platform must fetch the media from a public URL."""

    if kind == "text":
        return False
    if platform == "threads":
        return True
    return platform == "instagram" and kind in {"photo", "carousel"}


# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------


def preflight(
    targets: Sequence[Account], content: Content, creds: Credentials
) -> list[str]:
    """Every reason this run must not start. Empty means it is safe to go."""

    problems: list[str] = []
    videos = sum(1 for item in content.items if item.kind == "video")

    for account in targets:
        platform = account.platform
        limits = LIMITS[platform]
        label = f"{platform}:"

        if content.kind not in limits.kinds:
            problems.append(
                f"{label} cannot publish {content.kind} posts "
                f"(it accepts {', '.join(limits.kinds)})"
            )
            continue

        length = len(content.caption(platform))
        if length > limits.caption_chars:
            flag = "--text" if content.kind == "text" else "--caption"
            problems.append(
                f"{label} {length} chars, limit {limits.caption_chars} "
                f"(use {flag}-{platform})"
            )

        if content.kind == "photo" and len(content.items) > limits.photo_max:
            problems.append(
                f"{label} accepts at most {limits.photo_max} image(s) per post, "
                f"got {len(content.items)}"
            )
        if content.kind == "photo" and videos:
            problems.append(f"{label} photo posts cannot contain a video file")

        if content.kind == "carousel":
            count = len(content.items)
            if not limits.carousel_min <= count <= limits.carousel_max:
                problems.append(
                    f"{label} carousel takes {limits.carousel_min}-"
                    f"{limits.carousel_max} items, got {count}"
                )
            if videos and not limits.carousel_video:
                problems.append(f"{label} carousel cannot contain video")

        if content.kind == "video" and len(content.items) != 1:
            problems.append(f"{label} video posts take exactly one file")

        for item in content.items:
            cap = limits.max_bytes.get(item.kind, 0)
            if cap and item.size > cap:
                problems.append(
                    f"{label} {item.path.name} is "
                    f"{item.size / 1_048_576:.1f} MB, limit "
                    f"{cap // 1_048_576} MB for {item.kind}"
                )

        if platform == "youtube":
            title = content.title or ""
            if not title:
                problems.append(f"{label} --title is required")
            elif len(title) > YOUTUBE_TITLE_CHARS:
                problems.append(
                    f"{label} title is {len(title)} chars, limit "
                    f"{YOUTUBE_TITLE_CHARS}"
                )
            for field, value in (
                ("title", title),
                ("description", content.caption(platform)),
            ):
                if "<" in value or ">" in value:
                    problems.append(f"{label} {field} may not contain '<' or '>'")

        if content.cover and platform == "instagram" and creds.media_host is None:
            problems.append(
                f"{label} --cover is served from a public URL, but media_host "
                "is not configured in the credentials file"
            )

        if needs_media_host(platform, content.kind) and creds.media_host is None:
            problems.append(
                f"{label} needs a public URL for its media, but media_host is "
                "not configured in the credentials file"
            )

    return problems


def build_plan(targets: Sequence[Account], content: Content) -> list[PlanEntry]:
    entries = []
    for account in targets:
        platform = account.platform
        if content.kind == "text":
            route = "none"
        elif needs_media_host(platform, content.kind):
            route = "s3 then public url"
        else:
            route = UPLOAD_ROUTE[platform].get(content.items[0].kind, "upload")
        entry = PlanEntry(
            target=account.target,
            kind=content.kind,
            media=[item.describe() for item in content.items],
            caption_chars=len(content.caption(platform)),
            upload=route,
        )
        if platform == "youtube":
            entry.title = content.title
            entry.privacy = content.privacy
        entries.append(entry)
    return entries


# --------------------------------------------------------------------------
# Token refresh
# --------------------------------------------------------------------------


async def refresh_tokens(
    path: Path,
    creds: Credentials,
    targets: Sequence[Account],
    *,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Renew Meta tokens that are near expiry, rewriting the file in place."""

    now = datetime.now(timezone.utc)
    due = [
        account
        for account in targets
        if account.platform in REFRESHERS
        and (
            force
            or account.expires_at is None
            or account.expires_at - now < REFRESH_WINDOW
        )
    ]
    refreshed: list[dict[str, Any]] = []
    warnings: list[str] = []
    updates: dict[tuple[str, str], tuple[str, datetime]] = {}

    for account in due:
        token, expires, error = await REFRESHERS[account.platform](
            account, transport=transport
        )
        if token is None:
            warnings.append(f"{account.target}: token refresh failed ({error})")
            continue
        account.access_token = token
        account.expires_at = expires
        updates[(account.name, account.platform)] = (token, expires)
        refreshed.append(
            {
                "account": account.name,
                "platform": account.platform,
                "expires_at": expires.isoformat() if expires else None,
            }
        )

    if updates:
        with credentials_lock(path):
            _, stored = load_credentials(path)
            for account in stored.accounts:
                update = updates.get((account.name, account.platform))
                if update:
                    account.access_token, account.expires_at = update
            save_credentials(path, stored)
    return refreshed, warnings


# --------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------


async def publish(
    targets: Sequence[Account],
    content: Content,
    media: MediaHostSession,
    *,
    chat_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[PublishResult]:
    """Publish to every target in order, then remove all uploaded objects.

    Cleanup runs once at the end rather than per target: Instagram and Threads
    re-fetch media while a container is processing, and two targets can share
    the same uploaded object.
    """

    results: list[PublishResult] = []
    try:
        for account in targets:
            kwargs: dict[str, Any] = {"transport": transport}
            if account.platform == "telegram" and chat_id:
                kwargs["chat_id"] = chat_id
            client = CLIENTS[account.platform](account, media, **kwargs)
            try:
                results.append(await client.publish(content))
            except Exception as exc:  # noqa: BLE001 - one target must not abort the run
                results.append(
                    PublishResult.failed(
                        account.target, "unexpected_error", f"{type(exc).__name__}"
                    )
                )
    finally:
        await media.cleanup()
    return results


async def run(
    *,
    kind: Kind,
    files: Sequence[str],
    caption: str,
    overrides: dict[str, str],
    cover: str | None = None,
    title: str | None = None,
    tags: Sequence[str] = (),
    privacy: str = "public",
    account: str,
    platforms: Sequence[str],
    confirm: bool,
    chat_id: str | None = None,
    creds_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], bool]:
    """Resolve, validate, and either plan or publish. Returns (payload, ok)."""

    path, creds = load_credentials(creds_path)
    targets = resolve_targets(creds, account, platforms)
    content = build_content(
        kind, files, caption, overrides, cover, title=title, tags=tags, privacy=privacy
    )

    problems = preflight(targets, content, creds)
    if problems:
        return (
            {
                "status": "error",
                "reason": "preflight_failed",
                "detail": problems,
                "published": [],
            },
            False,
        )

    plan = [entry.model_dump() for entry in build_plan(targets, content)]
    if not confirm:
        return {"status": "dry_run", "plan": plan}, True

    refreshed, warnings = await refresh_tokens(
        path, creds, targets, transport=transport
    )
    media = MediaHostSession(creds.media_host, transport=transport)
    results = await publish(
        targets, content, media, chat_id=chat_id, transport=transport
    )

    published = [r for r in results if r.status == "published"]
    payload: dict[str, Any] = {
        "status": (
            "published"
            if len(published) == len(results)
            else "partial" if published else "error"
        ),
        "results": [r.model_dump(exclude_none=True) for r in results],
    }
    if refreshed:
        payload["refreshed"] = refreshed
    if warnings:
        payload["warnings"] = warnings
    return payload, len(published) == len(results)
