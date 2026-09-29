"""YouTube publishing over the Data API v3.

Publishing itself never touches a browser: the stored refresh token is
exchanged for a one-hour access token on every run. The browser is needed
exactly once, in `authorize`, to obtain that refresh token from the OAuth
client the user downloaded from Google Cloud.

Videos are sent through the resumable upload protocol in chunks, so a dropped
connection resumes from the last acknowledged byte instead of starting over.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets as secretlib
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from models import Content, PublishResult, YouTubeAccount
from utils import ConfigError, content_type, send

API = "YouTube API"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DATA = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
# Google requires resumable chunks to be multiples of 256 KiB.
CHUNK = 8 * 1024 * 1024
UPLOAD_RETRIES = 5


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


async def access_token(
    account: YouTubeAccount,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> tuple[str | None, str | None]:
    """Trade the refresh token for a short-lived access token."""

    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        payload, error = await send(
            client,
            "POST",
            TOKEN_URL,
            api=API,
            secrets=(account.client_secret, account.refresh_token),
            data={
                "grant_type": "refresh_token",
                "client_id": account.client_id,
                "client_secret": account.client_secret,
                "refresh_token": account.refresh_token,
            },
        )
    if payload is None:
        return None, error
    token = str(payload.get("access_token") or "")
    if not token:
        return None, "token endpoint returned no access_token"
    return token, None


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


class YouTubeClient:
    def __init__(
        self,
        account: YouTubeAccount,
        media: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.account = account
        self.transport = transport
        self.timeout = timeout
        self.target = account.target
        self.secrets = (account.client_secret, account.refresh_token)

    def _metadata(self, content: Content) -> dict[str, Any]:
        snippet: dict[str, Any] = {
            "title": content.title or "",
            "description": content.caption("youtube"),
        }
        if content.tags:
            snippet["tags"] = content.tags
        return {
            "snippet": snippet,
            "status": {
                "privacyStatus": content.privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

    async def _start(
        self, client: httpx.AsyncClient, token: str, content: Content
    ) -> tuple[str | None, str | None]:
        """Open a resumable session; returns (session url, error)."""

        item = content.items[0]
        try:
            response = await client.post(
                UPLOAD,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": content_type(item.path),
                    "X-Upload-Content-Length": str(item.size),
                },
                content=json.dumps(self._metadata(content), ensure_ascii=False),
            )
        except httpx.HTTPError as exc:
            return None, f"{API} request failed ({type(exc).__name__})"
        if not response.is_success:
            return None, _error_text(response)
        location = response.headers.get("Location")
        if not location:
            return None, "resumable session returned no Location"
        return location, None

    async def _status(
        self, client: httpx.AsyncClient, token: str, session: str, size: int
    ) -> tuple[int | None, dict[str, Any] | None, str | None]:
        """Ask how many bytes the session already holds.

        Returns (next offset, final payload, error); the payload is set only
        when the session had in fact already completed.
        """

        try:
            response = await client.put(
                session,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{size}",
                },
            )
        except httpx.HTTPError as exc:
            return None, None, f"{API} request failed ({type(exc).__name__})"
        if response.status_code == 308:
            return _received(response.headers.get("Range")), None, None
        if response.is_success:
            try:
                return size, response.json(), None
            except ValueError:
                return None, None, f"{API} returned an invalid response"
        return None, None, _error_text(response)

    async def _upload(
        self, client: httpx.AsyncClient, token: str, session: str, content: Content
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Stream the file in chunks, resuming after transient failures."""

        item = content.items[0]
        size = item.size
        offset = 0
        failures = 0
        mime = content_type(item.path)
        with open(item.path, "rb") as stream:
            while offset < size:
                stream.seek(offset)
                chunk = stream.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    response = await client.put(
                        session,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": mime,
                            "Content-Range": f"bytes {offset}-{end}/{size}",
                        },
                        content=chunk,
                    )
                except httpx.HTTPError:
                    response = None

                if response is not None and response.status_code == 308:
                    offset = _received(response.headers.get("Range"))
                    failures = 0
                    continue
                if response is not None and response.is_success:
                    try:
                        payload = response.json()
                    except ValueError:
                        return None, f"{API} returned an invalid response"
                    return payload, None
                if response is not None and response.status_code in (
                    400,
                    401,
                    403,
                    404,
                ):
                    return None, _error_text(response)

                failures += 1
                if failures > UPLOAD_RETRIES:
                    detail = (
                        _error_text(response)
                        if response is not None
                        else "connection dropped"
                    )
                    return None, f"upload gave up after {failures} failures: {detail}"
                received, payload, error = await self._status(
                    client, token, session, size
                )
                if received is None:
                    return None, error
                if payload is not None:
                    return payload, None
                offset = received
        return None, "upload loop ended without a final response"

    async def _thumbnail(
        self, client: httpx.AsyncClient, token: str, video_id: str, cover: Path
    ) -> str | None:
        """Best effort: needs a phone-verified channel, so failure is a note."""

        try:
            response = await client.post(
                THUMBNAIL,
                params={"videoId": video_id, "uploadType": "media"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type(cover),
                },
                content=cover.read_bytes(),
            )
        except httpx.HTTPError as exc:
            return f"thumbnail not set ({type(exc).__name__})"
        if not response.is_success:
            return f"thumbnail not set: {_error_text(response)}"
        return None

    async def publish(self, content: Content) -> PublishResult:
        token, error = await access_token(self.account, transport=self.transport)
        if token is None:
            return PublishResult.failed(self.target, "auth_failed", error)

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            session, error = await self._start(client, token, content)
            if session is None:
                return PublishResult.failed(self.target, "publish_failed", error)
            payload, error = await self._upload(client, token, session, content)
            if payload is None:
                return PublishResult.failed(self.target, "publish_failed", error)
            video_id = str(payload.get("id") or "")
            if not video_id:
                return PublishResult.failed(
                    self.target, "publish_failed", "response contains no video id"
                )
            note = None
            if content.cover:
                note = await self._thumbnail(client, token, video_id, content.cover)

        status = (payload.get("status") or {}).get("privacyStatus")
        detail_parts = []
        if status and status != content.privacy:
            detail_parts.append(
                f"YouTube stored the video as {status} instead of "
                f"{content.privacy} (unaudited API projects are forced to "
                "private; flip it in YouTube Studio)"
            )
        if note:
            detail_parts.append(note)
        return PublishResult(
            target=self.target,
            status="published",
            id=video_id,
            permalink=f"https://youtu.be/{video_id}",
            detail="; ".join(detail_parts) or None,
        )


def _received(range_header: str | None) -> int:
    """`Range: bytes=0-1048575` means the next byte to send is 1048576."""

    if not range_header or "-" not in range_header:
        return 0
    return int(range_header.split("-")[-1]) + 1


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or f"{API} returned status {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        reasons = [e.get("reason") for e in error.get("errors", []) if e.get("reason")]
        message = error.get("message") or f"status {response.status_code}"
        return f"{message} ({', '.join(reasons)})" if reasons else str(message)
    if isinstance(error, str):
        return f"{error}: {payload.get('error_description', '')}".strip(": ")
    return f"{API} returned status {response.status_code}"


# --------------------------------------------------------------------------
# One-time authorisation
# --------------------------------------------------------------------------


def load_client_secret(path: Path) -> tuple[str, str]:
    """Read the client id/secret from a Google Cloud OAuth client JSON."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read client secret file {path}: {exc}") from exc
    body = raw.get("installed") or raw.get("web") if isinstance(raw, dict) else None
    if not isinstance(body, dict) or not body.get("client_id"):
        raise ConfigError(
            f"{path} is not a Google OAuth client file "
            "(expected an 'installed' or 'web' section with client_id)"
        )
    if "installed" not in raw:
        raise ConfigError(
            "the OAuth client must be of type 'Desktop app' so the loopback "
            "redirect works; create one in Google Cloud → Credentials"
        )
    return str(body["client_id"]), str(body.get("client_secret") or "")


class _CodeCatcher(BaseHTTPRequestHandler):
    """Loopback endpoint that stores the authorisation code Google sends."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = parse_qs(urlparse(self.path).query)
        for key in ("code", "state", "error"):
            if key in query:
                _CodeCatcher.result[key] = query[key][0]
        body = (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>YouTube connected</h2><p>You can close this tab.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence the server
        return


def authorize(
    client_id: str,
    client_secret: str,
    *,
    timeout: float = 300.0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Run the loopback OAuth consent once and return the token payload.

    PKCE is used so the code cannot be replayed even if the client secret
    leaks; `prompt=consent` forces Google to issue a refresh token even when
    this account has authorised the client before.
    """

    verifier = base64.urlsafe_b64encode(secretlib.token_bytes(48)).rstrip(b"=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
    state = secretlib.token_urlsafe(16)

    _CodeCatcher.result = {}
    server = HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    port = server.server_address[1]
    redirect = f"http://localhost:{port}/"
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
                "code_challenge": challenge.decode("ascii"),
                "code_challenge_method": "S256",
            }
        )
    )
    print(f"Open this URL to authorise YouTube upload:\n{url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)

    thread.join(timeout)
    server.server_close()
    result = _CodeCatcher.result
    if thread.is_alive() or not result:
        raise ConfigError("no authorisation code arrived within the timeout")
    if result.get("error"):
        raise ConfigError(f"Google refused authorisation: {result['error']}")
    if result.get("state") != state:
        raise ConfigError("state mismatch on the OAuth callback")

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier.decode("ascii"),
        },
        timeout=30.0,
    )
    if not response.is_success:
        raise ConfigError(f"token exchange failed: {_error_text(response)}")
    payload = response.json()
    if not payload.get("refresh_token"):
        raise ConfigError(
            "Google returned no refresh_token; revoke the app at "
            "https://myaccount.google.com/permissions and run again"
        )
    return payload


def channel_info(token: str) -> dict[str, str]:
    """Who this token uploads as. Empty on any failure."""

    response = httpx.get(
        f"{DATA}/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if not response.is_success:
        return {}
    items = response.json().get("items") or []
    if not items:
        return {}
    return {
        "channel_id": str(items[0].get("id") or ""),
        "channel_title": str(items[0].get("snippet", {}).get("title") or ""),
    }
