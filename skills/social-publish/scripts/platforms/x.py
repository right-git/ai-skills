"""X publishing over the v2 API, authenticated with OAuth 1.0a.

The v1.1 upload endpoints were retired on 2025-06-09, so media goes through
`/2/media/upload/{initialize,append,finalize}` and the post through
`POST /2/tweets`. Bytes are uploaded directly; the media host is never used.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from authlib.oauth1 import ClientAuth

from models import Content, PublishResult, XAccount
from utils import content_type, send

API = "X API"
BASE = "https://api.x.com"
CHUNK = 4 * 1024 * 1024
STATUS_TIMEOUT = 300.0
CATEGORIES = {"image": "tweet_image", "video": "tweet_video"}


class _Auth(ClientAuth):
    """Sign with OAuth 1.0a, without authlib's `oauth_body_hash`.

    That parameter comes from an expired IETF draft which X does not
    implement; including it in the signature base string makes the v2 media
    endpoints reject the request as a bare 401. Every request we send to X
    carries a JSON or multipart body, and neither is signed under RFC 5849,
    so dropping the body from signing is correct here.
    """

    def sign(self, method, uri, headers, body):
        uri, headers, _ = super().sign(method, uri, dict(headers or {}), b"")
        return uri, headers, body


def auth_header(account: XAccount, method: str, url: str) -> dict[str, str]:
    """Authorization header for one request. `url` must carry its query."""

    auth = _Auth(
        account.consumer_key,
        account.consumer_secret,
        token=account.access_token,
        token_secret=account.access_token_secret,
        signature_method="HMAC-SHA1",
    )
    _, headers, _ = auth.sign(method.upper(), url, {}, b"")
    return {"Authorization": headers["Authorization"]}


class XClient:
    def __init__(
        self,
        account: XAccount,
        media: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.account = account
        self.transport = transport
        self.timeout = timeout
        self.target = account.target
        self.secrets = (account.access_token, account.access_token_secret)

    async def _send(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ):
        return await send(
            client,
            method,
            url,
            api=API,
            secrets=self.secrets,
            headers=auth_header(self.account, method, url),
            **kwargs,
        )

    # -- media ------------------------------------------------------------

    async def _upload(
        self, client: httpx.AsyncClient, item
    ) -> tuple[str | None, str | None]:
        body = item.path.read_bytes()
        payload, error = await self._send(
            client,
            "POST",
            f"{BASE}/2/media/upload/initialize",
            json={
                "media_type": content_type(item.path),
                "total_bytes": len(body),
                "media_category": CATEGORIES[item.kind],
            },
        )
        media_id = str((payload or {}).get("data", {}).get("id") or "")
        if not media_id:
            return None, error or "initialize returned no media id"

        for index, start in enumerate(range(0, len(body), CHUNK)):
            _, error = await self._send(
                client,
                "POST",
                f"{BASE}/2/media/upload/{media_id}/append",
                files={
                    "media": (
                        item.path.name,
                        body[start : start + CHUNK],
                        "application/octet-stream",
                    )
                },
                data={"segment_index": str(index)},
            )
            if error is not None:
                return None, f"segment {index}: {error}"

        payload, error = await self._send(
            client, "POST", f"{BASE}/2/media/upload/{media_id}/finalize"
        )
        if payload is None:
            return None, error
        info = payload.get("data", {}).get("processing_info")
        if info:
            error = await self._wait(client, media_id, info)
            if error is not None:
                return None, error
        return media_id, None

    async def _wait(
        self, client: httpx.AsyncClient, media_id: str, info: dict[str, Any]
    ) -> str | None:
        waited = 0.0
        while True:
            state = str(info.get("state") or "")
            if state == "succeeded":
                return None
            if state == "failed":
                error = info.get("error") or {}
                return str(error.get("message") or "media processing failed")
            delay = float(info.get("check_after_secs") or 5)
            if waited + delay > STATUS_TIMEOUT:
                return "media processing timed out"
            await asyncio.sleep(delay)
            waited += delay
            url = f"{BASE}/2/media/upload?command=STATUS&media_id={media_id}"
            payload, error = await self._send(client, "GET", url)
            if payload is None:
                return error
            info = payload.get("data", {}).get("processing_info") or {
                "state": "succeeded"
            }

    # -- publish ----------------------------------------------------------

    async def publish(self, content: Content) -> PublishResult:
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            media_ids: list[str] = []
            for index, item in enumerate(content.items, start=1):
                media_id, error = await self._upload(client, item)
                if media_id is None:
                    return PublishResult.failed(
                        self.target, "media_upload_failed", f"item {index}: {error}"
                    )
                media_ids.append(media_id)

            body: dict[str, Any] = {"text": content.caption("x")}
            if media_ids:
                body["media"] = {"media_ids": media_ids}
            payload, error = await self._send(
                client, "POST", f"{BASE}/2/tweets", json=body
            )
            post_id = str((payload or {}).get("data", {}).get("id") or "")
            if not post_id:
                return PublishResult.failed(self.target, "publish_failed", error)
            return PublishResult(
                target=self.target,
                status="published",
                id=post_id,
                permalink=f"https://x.com/i/web/status/{post_id}",
            )
