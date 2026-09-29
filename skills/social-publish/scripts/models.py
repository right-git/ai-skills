"""Credential, plan and result models shared by every platform client."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["instagram", "threads", "x", "telegram", "youtube"]
Kind = Literal["text", "photo", "video", "carousel"]
MediaKind = Literal["image", "video"]

PLATFORMS: tuple[Platform, ...] = ("instagram", "threads", "x", "telegram", "youtube")
Privacy = Literal["public", "unlisted", "private"]
YOUTUBE_TITLE_CHARS = 100


class PlatformLimits(BaseModel):
    """What one platform will accept. Enforced in api.preflight."""

    kinds: tuple[Kind, ...]
    caption_chars: int
    photo_max: int = 1
    carousel_min: int = 0
    carousel_max: int = 0
    carousel_video: bool = True
    # Hard upload caps per media kind, in bytes. Absent means unchecked.
    max_bytes: dict[str, int] = Field(default_factory=dict)


LIMITS: dict[Platform, PlatformLimits] = {
    "instagram": PlatformLimits(
        kinds=("photo", "video", "carousel"),
        caption_chars=2200,
        carousel_min=2,
        carousel_max=10,
    ),
    "threads": PlatformLimits(
        kinds=("text", "photo", "video", "carousel"),
        caption_chars=500,
        carousel_min=2,
        carousel_max=20,
    ),
    "x": PlatformLimits(
        kinds=("text", "photo", "video"),
        caption_chars=280,
        photo_max=4,
        max_bytes={"image": 5 * 1024 * 1024, "video": 512 * 1024 * 1024},
    ),
    "telegram": PlatformLimits(
        kinds=("text", "photo", "video", "carousel"),
        caption_chars=1024,
        carousel_min=2,
        carousel_max=10,
        carousel_video=True,
        max_bytes={"image": 10 * 1024 * 1024, "video": 50 * 1024 * 1024},
    ),
    # Description limit; the 100-char title is checked separately in preflight.
    "youtube": PlatformLimits(
        kinds=("video",),
        caption_chars=5000,
        max_bytes={"video": 256 * 1024 * 1024 * 1024},
    ),
}


class MediaHost(BaseModel):
    """S3-compatible bucket used to expose media Meta must fetch itself."""

    # Extras are preserved: token refresh rewrites this file in place, and
    # anything we did not model must survive the round-trip.
    model_config = ConfigDict(extra="allow")

    endpoint: str
    bucket: str
    public_base: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"


class BaseAccount(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str

    @property
    def target(self) -> str:
        return f"{self.name}:{self.platform}"  # type: ignore[attr-defined]


class InstagramAccount(BaseAccount):
    platform: Literal["instagram"]
    access_token: str
    # Both are optional: the user id is resolved from the token on first use,
    # and expires_at is written back by the refresh that renews the token.
    user_id: str | None = None
    expires_at: datetime | None = None


class ThreadsAccount(BaseAccount):
    platform: Literal["threads"]
    access_token: str
    user_id: str | None = None
    expires_at: datetime | None = None


class XAccount(BaseAccount):
    platform: Literal["x"]
    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str


class TelegramAccount(BaseAccount):
    platform: Literal["telegram"]
    bot_token: str
    chat_id: str


class YouTubeAccount(BaseAccount):
    """Written by `publish.py youtube-auth`; the refresh token never expires
    while the Google Cloud project stays "In production"."""

    platform: Literal["youtube"]
    client_id: str
    client_secret: str
    refresh_token: str
    channel_id: str | None = None
    channel_title: str | None = None


Account = Annotated[
    InstagramAccount | ThreadsAccount | XAccount | TelegramAccount | YouTubeAccount,
    Field(discriminator="platform"),
]


class Credentials(BaseModel):
    model_config = ConfigDict(extra="allow")

    accounts: list[Account]
    media_host: MediaHost | None = None


class MediaItem(BaseModel):
    path: Path
    kind: MediaKind
    size: int = 0
    width: int | None = None
    height: int | None = None
    duration: float | None = None

    def describe(self) -> str:
        parts = [self.path.name, f"{self.size / 1_048_576:.1f} MB"]
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.duration:
            parts.append(f"{self.duration:.1f}s")
        return f"{parts[0]} ({', '.join(parts[1:])})"


class Content(BaseModel):
    """One post, with its caption already resolved per platform."""

    kind: Kind
    captions: dict[str, str] = Field(default_factory=dict)
    items: list[MediaItem] = Field(default_factory=list)
    cover: Path | None = None
    # YouTube only: a video is a titled resource, not a captioned post.
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    privacy: Privacy = "public"

    def caption(self, platform: str) -> str:
        return self.captions.get(platform, "")


class PlanEntry(BaseModel):
    target: str
    kind: Kind
    media: list[str] = Field(default_factory=list)
    caption_chars: int = 0
    upload: str = "none"
    title: str | None = None
    privacy: str | None = None


class PublishResult(BaseModel):
    target: str
    status: Literal["published", "error", "skipped"]
    id: str | None = None
    permalink: str | None = None
    reason: str | None = None
    detail: str | None = None

    @classmethod
    def failed(cls, target: str, reason: str, detail: str | None = None):
        return cls(target=target, status="error", reason=reason, detail=detail)
