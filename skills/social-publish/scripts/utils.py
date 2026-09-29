"""Credential IO, request signing, media probing and the S3 media host."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from models import Credentials, MediaHost, MediaItem, MediaKind

ENV_VAR = "SOCIAL_CREDENTIALS_PATH"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}


class ConfigError(RuntimeError):
    """Credentials are missing, unreadable or malformed."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def credentials_path() -> Path:
    load_dotenv()
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        raise ConfigError(f"{ENV_VAR} is not set (add it to the repo-root .env)")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(f"{ENV_VAR} points at a missing file: {path}")
    return path


def load_credentials(path: Path | None = None) -> tuple[Path, Credentials]:
    path = path or credentials_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    try:
        creds = Credentials.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is not a valid credentials file:\n{exc}") from exc
    seen: set[tuple[str, str]] = set()
    for account in creds.accounts:
        key = (account.name, account.platform)
        if key in seen:
            raise ConfigError(f"duplicate account {key[0]!r} for platform {key[1]!r}")
        seen.add(key)
    return path, creds


@contextmanager
def credentials_lock(path: Path) -> Iterator[None]:
    """Serialise credential rewrites so concurrent runs cannot clobber tokens."""

    lock = path.with_suffix(path.suffix + ".lock")
    with open(lock, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save_credentials(path: Path, creds: Credentials) -> None:
    """Rewrite the credentials file atomically, preserving mode."""

    payload = creds.model_dump(mode="json", exclude_none=True)
    directory = path.parent
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".creds-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# Redaction and HTTP
# --------------------------------------------------------------------------


def redact(value: Any, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:1000]


def error_from(response: httpx.Response, api: str, secrets: Iterable[str]) -> str:
    """Best available human-readable error, with every secret stripped."""

    text = redact(response.text, secrets)
    try:
        payload = json.loads(text)
    except ValueError:
        return text or f"{api} returned status {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("error_user_msg") or error.get("message")
            if message:
                return redact(message, secrets)
        if isinstance(error, str) and error:
            return redact(error, secrets)
        for key in ("description", "detail", "error_message", "title"):
            if payload.get(key):
                return redact(payload[key], secrets)
        if payload.get("errors"):
            return redact(payload["errors"], secrets)
    return f"{api} returned status {response.status_code}"


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    api: str,
    secrets: Sequence[str] = (),
    **kwargs: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """One request, returning (payload, error). Never raises for HTTP failures."""

    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        return None, f"{api} request failed ({type(exc).__name__}): " + redact(
            exc, secrets
        )
    if not response.is_success:
        return None, error_from(response, api, secrets)
    if not response.content:
        return {}, None
    try:
        payload = response.json()
    except ValueError:
        return None, f"{api} returned an invalid response"
    if not isinstance(payload, dict):
        return None, f"{api} returned an invalid response"
    return payload, None


# --------------------------------------------------------------------------
# Media inspection
# --------------------------------------------------------------------------


def media_kind(path: Path) -> MediaKind | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "video/mp4" if media_kind(path) == "video" else "application/octet-stream"


def probe(path: Path) -> MediaItem:
    """Describe a local media file; ffprobe failures degrade to size only."""

    kind = media_kind(path)
    if kind is None:
        raise ConfigError(f"unsupported media type: {path.name}")
    item = MediaItem(path=path, kind=kind, size=path.stat().st_size)
    try:
        raw = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        data = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return item
    streams = [s for s in data.get("streams", []) if s.get("width")]
    if streams:
        item.width = streams[0].get("width")
        item.height = streams[0].get("height")
    duration = data.get("format", {}).get("duration")
    if duration:
        try:
            item.duration = float(duration)
        except ValueError:
            pass
    return item


# --------------------------------------------------------------------------
# AWS SigV4
# --------------------------------------------------------------------------


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    payload_hash: str,
    headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Sign a request with AWS Signature Version 4 and return all headers."""

    parsed = urlparse(url)
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    signed = {k.lower(): " ".join(str(v).split()) for k, v in (headers or {}).items()}
    signed.setdefault("host", parsed.netloc)
    signed.setdefault("x-amz-date", amz_date)
    if service == "s3":
        signed.setdefault("x-amz-content-sha256", payload_hash)

    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)

    query = "&".join(
        sorted(
            f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
            for k, _, v in (p.partition("=") for p in parsed.query.split("&") if p)
        )
    )
    canonical_request = "\n".join(
        [
            method.upper(),
            quote(parsed.path or "/", safe="/-_.~"),
            query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = f"AWS4{secret_key}".encode("utf-8")
    for part in (datestamp, region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(
        key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        **{name: signed[name] for name in names if name != "host"},
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


# --------------------------------------------------------------------------
# Media host
# --------------------------------------------------------------------------


class MediaHostSession:
    """Uploads local media so Meta can fetch it, then removes every object.

    Objects are deleted only once the whole run is over: Instagram and Threads
    re-fetch media while a container is still processing, so a per-target
    cleanup would break a second target still pointing at the same URL.
    """

    def __init__(
        self,
        host: MediaHost | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.host = host
        self.transport = transport
        self.timeout = timeout
        self._urls: dict[Path, str] = {}
        self._keys: list[str] = []

    @property
    def configured(self) -> bool:
        return self.host is not None

    def _object_url(self, key: str) -> str:
        host = self.host
        assert host is not None
        return f"{host.endpoint.rstrip('/')}/{host.bucket}/{key}"

    def public_url(self, key: str) -> str:
        host = self.host
        assert host is not None
        return f"{host.public_base.rstrip('/')}/{key}"

    async def url_for(self, path: Path) -> tuple[str | None, str | None]:
        """Return a public URL for a local file, uploading it once per run."""

        if path in self._urls:
            return self._urls[path], None
        host = self.host
        if host is None:
            return None, (
                "media_host is not configured in the credentials file; "
                "Instagram photos and all Threads media need a public URL"
            )
        body = path.read_bytes()
        key = f"social/{uuid.uuid4().hex}{path.suffix.lower()}"
        url = self._object_url(key)
        headers = sigv4_headers(
            method="PUT",
            url=url,
            region=host.region,
            service="s3",
            access_key=host.access_key_id,
            secret_key=host.secret_access_key,
            payload_hash=hashlib.sha256(body).hexdigest(),
            headers={"content-type": content_type(path)},
        )
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            _, error = await send(
                client,
                "PUT",
                url,
                api="Media host",
                secrets=(host.secret_access_key, host.access_key_id),
                headers=headers,
                content=body,
            )
        if error is not None:
            return None, error
        self._keys.append(key)
        public = self.public_url(key)
        self._urls[path] = public
        return public, None

    async def cleanup(self) -> None:
        """Delete every object this run uploaded. Best effort, never raises."""

        host = self.host
        if host is None or not self._keys:
            return
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            for key in self._keys:
                url = self._object_url(key)
                headers = sigv4_headers(
                    method="DELETE",
                    url=url,
                    region=host.region,
                    service="s3",
                    access_key=host.access_key_id,
                    secret_key=host.secret_access_key,
                    payload_hash=hashlib.sha256(b"").hexdigest(),
                )
                await send(
                    client,
                    "DELETE",
                    url,
                    api="Media host",
                    secrets=(host.secret_access_key,),
                    headers=headers,
                )
        self._keys.clear()
        self._urls.clear()
