"""CLI for publishing one post to one or more social accounts.

Dry-run is the default: nothing reaches a network without --confirm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import LIMITS, PLATFORMS, YOUTUBE_TITLE_CHARS  # noqa: E402
from utils import (  # noqa: E402
    ConfigError,
    credentials_lock,
    load_credentials,
    save_credentials,
)
import api  # noqa: E402
from platforms import youtube  # noqa: E402

MEDIA_KINDS = ("photo", "video", "carousel")


def _emit(payload: dict, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def _add_targeting(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, help="account name in creds.json")
    parser.add_argument(
        "--platform",
        required=True,
        help=f"comma-separated: {', '.join(PLATFORMS)} — or 'all'",
    )
    parser.add_argument("--creds", help="override SOCIAL_CREDENTIALS_PATH")
    parser.add_argument("--chat-id", help="Telegram chat override")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually publish (without it you get a dry-run plan)",
    )


def _add_captions(parser: argparse.ArgumentParser, flag: str) -> None:
    parser.add_argument(f"--{flag}", default="", help="text used for every platform")
    for platform in PLATFORMS:
        parser.add_argument(
            f"--{flag}-{platform}",
            dest=f"{flag}_{platform}",
            help=f"override for {platform} (limit {LIMITS[platform].caption_chars})",
        )


def _limits_help(kind: str) -> str:
    accepted = [p for p in PLATFORMS if kind in LIMITS[p].kinds]
    parts = []
    for platform in accepted:
        limits = LIMITS[platform]
        detail = f"{platform} <={limits.caption_chars} chars"
        if kind == "carousel":
            detail += f", {limits.carousel_min}-{limits.carousel_max} items"
        if kind == "photo" and limits.photo_max > 1:
            detail += f", <={limits.photo_max} images"
        parts.append(detail)
    rejected = [p for p in PLATFORMS if kind not in LIMITS[p].kinds]
    text = " | ".join(parts)
    if rejected:
        text += f" | NOT supported by: {', '.join(rejected)}"
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish.py",
        description="Publish to Instagram, Threads, X, Telegram and YouTube",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    text = subs.add_parser("text", help=f"text-only post — {_limits_help('text')}")
    _add_targeting(text)
    _add_captions(text, "text")

    for kind in MEDIA_KINDS:
        sub = subs.add_parser(kind, help=f"{kind} post — {_limits_help(kind)}")
        _add_targeting(sub)
        _add_captions(sub, "caption")
        sub.add_argument(
            "--file",
            action="append",
            required=True,
            dest="files",
            help="local media path (repeat for carousels and X image sets)",
        )
        if kind == "video":
            sub.add_argument(
                "--cover", help="Instagram Reel cover / YouTube thumbnail image"
            )
            sub.add_argument(
                "--title",
                help=(
                    "YouTube video title (required there, "
                    f"<={YOUTUBE_TITLE_CHARS} chars)"
                ),
            )
            sub.add_argument(
                "--tags",
                default="",
                help="YouTube tags, comma-separated",
            )
            sub.add_argument(
                "--privacy",
                default="public",
                choices=("public", "unlisted", "private"),
                help="YouTube privacy status (default public)",
            )

    listing = subs.add_parser("accounts", help="list configured targets")
    listing.add_argument("--creds")

    renew = subs.add_parser("refresh", help="renew Instagram and Threads tokens")
    renew.add_argument("--creds")
    renew.add_argument("--account", help="limit to one account name")

    yt = subs.add_parser(
        "youtube-auth",
        help="one-time browser consent: store a YouTube refresh token",
    )
    yt.add_argument("--account", required=True, help="account name to attach to")
    yt.add_argument(
        "--client-secret",
        required=True,
        help="client_secret_*.json downloaded from Google Cloud (Desktop app)",
    )
    yt.add_argument("--creds")
    yt.add_argument(
        "--no-browser",
        action="store_true",
        help="only print the URL instead of opening the default browser",
    )

    return parser


def cmd_accounts(args: argparse.Namespace) -> int:
    _, creds = load_credentials(Path(args.creds) if args.creds else None)
    rows: dict[str, list[str]] = {}
    for account in creds.accounts:
        rows.setdefault(account.name, []).append(account.platform)
    _emit(
        {
            "media_host": bool(creds.media_host),
            "accounts": [
                {"name": name, "platforms": sorted(platforms)}
                for name, platforms in sorted(rows.items())
            ],
        }
    )
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    path, creds = load_credentials(Path(args.creds) if args.creds else None)
    targets = [
        a
        for a in creds.accounts
        if a.platform in api.REFRESHERS and (not args.account or a.name == args.account)
    ]
    refreshed, warnings = asyncio.run(
        api.refresh_tokens(path, creds, targets, force=True)
    )
    payload = {"refreshed": refreshed}
    if warnings:
        payload["warnings"] = warnings
    _emit(payload, error=bool(warnings))
    return 1 if warnings else 0


def cmd_youtube_auth(args: argparse.Namespace) -> int:
    path, creds = load_credentials(Path(args.creds) if args.creds else None)
    client_id, client_secret = youtube.load_client_secret(
        Path(args.client_secret).expanduser()
    )
    tokens = youtube.authorize(
        client_id, client_secret, open_browser=not args.no_browser
    )
    info = youtube.channel_info(tokens["access_token"])
    entry = {
        "name": args.account,
        "platform": "youtube",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        **info,
    }
    with credentials_lock(path):
        _, stored = load_credentials(path)
        raw = stored.model_dump(mode="json", exclude_none=True)
        accounts = [
            a
            for a in raw["accounts"]
            if not (a["name"] == args.account and a["platform"] == "youtube")
        ]
        accounts.append(entry)
        raw["accounts"] = accounts
        save_credentials(path, type(stored).model_validate(raw))
    _emit(
        {
            "status": "authorized",
            "account": args.account,
            "platform": "youtube",
            "scopes": list(youtube.SCOPES),
            **info,
            "credentials": str(path),
        }
    )
    return 0


def cmd_publish(args: argparse.Namespace, kind: str) -> int:
    flag = "text" if kind == "text" else "caption"
    overrides = {
        platform: value
        for platform in PLATFORMS
        if (value := getattr(args, f"{flag}_{platform}")) is not None
    }
    payload, ok = asyncio.run(
        api.run(
            kind=kind,
            files=getattr(args, "files", []) or [],
            caption=getattr(args, flag),
            overrides=overrides,
            cover=getattr(args, "cover", None),
            title=getattr(args, "title", None),
            tags=[t for t in (getattr(args, "tags", "") or "").split(",")],
            privacy=getattr(args, "privacy", "public"),
            account=args.account,
            platforms=[p.strip() for p in args.platform.split(",") if p.strip()],
            confirm=args.confirm,
            chat_id=args.chat_id,
            creds_path=Path(args.creds) if args.creds else None,
        )
    )
    _emit(payload, error=not ok)
    if ok:
        return 0
    return 2 if payload.get("reason") == "preflight_failed" else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "accounts":
            return cmd_accounts(args)
        if args.command == "refresh":
            return cmd_refresh(args)
        if args.command == "youtube-auth":
            return cmd_youtube_auth(args)
        return cmd_publish(args, args.command)
    except ConfigError as exc:
        _emit(
            {"status": "error", "reason": "configuration_error", "detail": str(exc)},
            error=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
