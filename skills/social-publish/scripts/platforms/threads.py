"""Threads publishing over the Threads Graph API.

Threads accepts no file uploads at all: every image and video is fetched by
Meta from the URL we hand it, so all media goes through the media host.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from models import Content, PublishResult, ThreadsAccount
from utils import MediaHostSession, send

API = "Threads API"
BASE = "https://graph.threads.net/v1.0"
REFRESH = "https://graph.threads.net/refresh_access_token"

POLL_TIMEOUT = 300.0
POLL_INTERVAL = 3.0
POLL_MAX_INTERVAL = 15.0


async def refresh(
    account: ThreadsAccount,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> tuple[str | None, datetime | None, str | None]:
    """Exchange a long-lived token for a fresh one. No browser involved."""

    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        payload, error = await send(
            client,
            "GET",
            REFRESH,
            api=API,
            secrets=(account.access_token,),
            params={
                "grant_type": "th_refresh_token",
                "access_token": account.access_token,
            },
        )
    if payload is None:
        return None, None, error
    token = str(payload.get("access_token") or "")
    if not token:
        return None, None, "refresh returned no access_token"
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=int(payload.get("expires_in") or 0)
    )
    return token, expires, None


class ThreadsClient:
    def __init__(
        self,
        account: ThreadsAccount,
        media: MediaHostSession,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.account = account
        self.media = media
        self.transport = transport
        self.timeout = timeout
        self.target = account.target
        self.secrets = (account.access_token,)
        self._uid = account.user_id

    # -- plumbing ---------------------------------------------------------

    async def _send(
        self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ):
        return await send(
            client,
            method,
            f"{BASE}/{path.lstrip('/')}",
            api=API,
            secrets=self.secrets,
            headers={"Authorization": f"Bearer {self.account.access_token}"},
            **kwargs,
        )

    async def _user_id(self, client: httpx.AsyncClient) -> str | None:
        """Resolve the publishing id from the token unless creds pin one."""

        if self._uid:
            return None
        payload, error = await self._send(client, "GET", "me", params={"fields": "id"})
        if payload is None:
            return error
        uid = str(payload.get("id") or "")
        if not uid:
            return "profile response contains no user id"
        self._uid = uid
        return None

    async def _container(
        self, client: httpx.AsyncClient, data: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        payload, error = await self._send(
            client, "POST", f"{self._uid}/threads", data=data
        )
        container_id = str((payload or {}).get("id") or "")
        if not container_id:
            return None, error or "container response contains no id"
        return container_id, None

    async def _wait(
        self, client: httpx.AsyncClient, container_id: str
    ) -> tuple[bool, str | None]:
        deadline = time.monotonic() + POLL_TIMEOUT
        interval = POLL_INTERVAL
        while True:
            payload, error = await self._send(
                client,
                "GET",
                container_id,
                params={"fields": "status,error_message"},
            )
            if payload is None:
                return False, error
            status = str(payload.get("status") or "").upper()
            if status == "FINISHED":
                return True, None
            if status in {"ERROR", "EXPIRED"}:
                return False, str(payload.get("error_message") or status)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "media container processing timed out"
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * 1.5, POLL_MAX_INTERVAL)

    async def _media_data(self, item) -> tuple[dict[str, Any] | None, str | None]:
        url, error = await self.media.url_for(item.path)
        if url is None:
            return None, error
        if item.kind == "video":
            return {"media_type": "VIDEO", "video_url": url}, None
        return {"media_type": "IMAGE", "image_url": url}, None

    async def _finish(
        self, client: httpx.AsyncClient, container_id: str, *, wait: bool
    ) -> PublishResult:
        if wait:
            ok, error = await self._wait(client, container_id)
            if not ok:
                return PublishResult.failed(self.target, "container_failed", error)
        payload, error = await self._send(
            client,
            "POST",
            f"{self._uid}/threads_publish",
            data={"creation_id": container_id},
        )
        post_id = str((payload or {}).get("id") or "")
        if not post_id:
            return PublishResult.failed(self.target, "publish_failed", error)
        info, _ = await self._send(
            client, "GET", post_id, params={"fields": "permalink"}
        )
        return PublishResult(
            target=self.target,
            status="published",
            id=post_id,
            permalink=(info or {}).get("permalink"),
        )

    # -- kinds ------------------------------------------------------------

    async def _publish_single(
        self, client: httpx.AsyncClient, content: Content
    ) -> PublishResult:
        data: dict[str, Any] = {"text": content.caption("threads")}
        wait = False
        if content.kind == "text":
            data["media_type"] = "TEXT"
        else:
            media, error = await self._media_data(content.items[0])
            if media is None:
                return PublishResult.failed(self.target, "media_host_failed", error)
            data.update(media)
            wait = True
        container_id, error = await self._container(client, data)
        if container_id is None:
            return PublishResult.failed(self.target, "container_create_failed", error)
        return await self._finish(client, container_id, wait=wait)

    async def _publish_carousel(
        self, client: httpx.AsyncClient, content: Content
    ) -> PublishResult:
        children: list[str] = []
        for index, item in enumerate(content.items, start=1):
            media, error = await self._media_data(item)
            if media is None:
                return PublishResult.failed(self.target, "media_host_failed", error)
            child_id, error = await self._container(
                client, {**media, "is_carousel_item": "true"}
            )
            if child_id is None:
                return PublishResult.failed(
                    self.target, "carousel_item_failed", f"item {index}: {error}"
                )
            ok, error = await self._wait(client, child_id)
            if not ok:
                return PublishResult.failed(
                    self.target, "carousel_item_failed", f"item {index}: {error}"
                )
            children.append(child_id)

        container_id, error = await self._container(
            client,
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "text": content.caption("threads"),
            },
        )
        if container_id is None:
            return PublishResult.failed(self.target, "container_create_failed", error)
        return await self._finish(client, container_id, wait=True)

    async def publish(self, content: Content) -> PublishResult:
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            error = await self._user_id(client)
            if error is not None:
                return PublishResult.failed(self.target, "account_lookup_failed", error)
            if content.kind == "carousel":
                return await self._publish_carousel(client, content)
            return await self._publish_single(client, content)
