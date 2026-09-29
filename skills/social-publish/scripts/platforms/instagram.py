"""Instagram publishing over the Instagram Login Graph API.

Reels upload their bytes directly through the resumable endpoint; photos and
every carousel child must be fetched by Meta from a public URL, so those go
through the media host first.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from models import Content, InstagramAccount, PublishResult
from utils import MediaHostSession, content_type, send

API = "Instagram API"
VERSION = "v23.0"
BASE = f"https://graph.instagram.com/{VERSION}"
UPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
REFRESH = "https://graph.instagram.com/refresh_access_token"

POLL_TIMEOUT = 300.0
POLL_INTERVAL = 3.0
POLL_MAX_INTERVAL = 15.0
DONE = {"FINISHED", "PUBLISHED"}


async def refresh(
    account: InstagramAccount,
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
                "grant_type": "ig_refresh_token",
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


class InstagramClient:
    def __init__(
        self,
        account: InstagramAccount,
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
        payload, error = await self._send(
            client, "GET", "me", params={"fields": "user_id,id"}
        )
        if payload is None:
            return error
        uid = str(payload.get("user_id") or payload.get("id") or "")
        if not uid:
            return "profile response contains no user id"
        self._uid = uid
        return None

    async def _container(
        self, client: httpx.AsyncClient, data: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        payload, error = await self._send(
            client, "POST", f"{self._uid}/media", data=data
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
                params={"fields": "status_code,status"},
            )
            if payload is None:
                return False, error
            status = str(payload.get("status_code") or "").upper()
            if status in DONE:
                return True, None
            if status in {"ERROR", "EXPIRED"}:
                return False, str(payload.get("status") or status)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "media container processing timed out"
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * 1.5, POLL_MAX_INTERVAL)

    async def _upload_bytes(
        self, client: httpx.AsyncClient, container_id: str, content: Content
    ) -> str | None:
        item = content.items[0]
        body = item.path.read_bytes()
        _, error = await send(
            client,
            "POST",
            f"{UPLOAD}/{container_id}",
            api=API,
            secrets=self.secrets,
            headers={
                "Authorization": f"OAuth {self.account.access_token}",
                "offset": "0",
                "file_size": str(len(body)),
                "Content-Type": content_type(item.path),
            },
            content=body,
        )
        return error

    async def _finish(
        self, client: httpx.AsyncClient, container_id: str
    ) -> PublishResult:
        ok, error = await self._wait(client, container_id)
        if not ok:
            return PublishResult.failed(self.target, "container_failed", error)
        payload, error = await self._send(
            client,
            "POST",
            f"{self._uid}/media_publish",
            data={"creation_id": container_id},
        )
        media_id = str((payload or {}).get("id") or "")
        if not media_id:
            return PublishResult.failed(self.target, "publish_failed", error)
        info, _ = await self._send(
            client, "GET", media_id, params={"fields": "permalink"}
        )
        return PublishResult(
            target=self.target,
            status="published",
            id=media_id,
            permalink=(info or {}).get("permalink"),
        )

    # -- kinds ------------------------------------------------------------

    async def _publish_photo(
        self, client: httpx.AsyncClient, content: Content
    ) -> PublishResult:
        url, error = await self.media.url_for(content.items[0].path)
        if url is None:
            return PublishResult.failed(self.target, "media_host_failed", error)
        data = {"image_url": url, "caption": content.caption("instagram")}
        container_id, error = await self._container(client, data)
        if container_id is None:
            return PublishResult.failed(self.target, "container_create_failed", error)
        return await self._finish(client, container_id)

    async def _publish_video(
        self, client: httpx.AsyncClient, content: Content
    ) -> PublishResult:
        data = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": content.caption("instagram"),
        }
        if content.cover:
            cover_url, error = await self.media.url_for(content.cover)
            if cover_url is None:
                return PublishResult.failed(self.target, "media_host_failed", error)
            data["cover_url"] = cover_url
        container_id, error = await self._container(client, data)
        if container_id is not None:
            error = await self._upload_bytes(client, container_id, content)
            if error is not None:
                return PublishResult.failed(self.target, "upload_failed", error)
            return await self._finish(client, container_id)

        # Не всякий аккаунт принимает resumable-загрузку: часть отвечает
        # «The parameter video_url is required» и ждёт публичную ссылку.
        # Тогда ролик уезжает на media_host, как фото и элементы карусели.
        if "video_url" not in (error or ""):
            return PublishResult.failed(self.target, "container_create_failed", error)
        url, host_error = await self.media.url_for(content.items[0].path)
        if url is None:
            return PublishResult.failed(self.target, "media_host_failed", host_error)
        data.pop("upload_type", None)
        data["video_url"] = url
        container_id, error = await self._container(client, data)
        if container_id is None:
            return PublishResult.failed(self.target, "container_create_failed", error)
        return await self._finish(client, container_id)

    async def _publish_carousel(
        self, client: httpx.AsyncClient, content: Content
    ) -> PublishResult:
        children: list[str] = []
        for index, item in enumerate(content.items, start=1):
            url, error = await self.media.url_for(item.path)
            if url is None:
                return PublishResult.failed(self.target, "media_host_failed", error)
            data: dict[str, Any] = {"is_carousel_item": "true"}
            if item.kind == "video":
                data["media_type"] = "VIDEO"
                data["video_url"] = url
            else:
                data["image_url"] = url
            child_id, error = await self._container(client, data)
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
                "caption": content.caption("instagram"),
            },
        )
        if container_id is None:
            return PublishResult.failed(self.target, "container_create_failed", error)
        return await self._finish(client, container_id)

    async def publish(self, content: Content) -> PublishResult:
        handlers = {
            "photo": self._publish_photo,
            "video": self._publish_video,
            "carousel": self._publish_carousel,
        }
        handler = handlers[content.kind]
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            error = await self._user_id(client)
            if error is not None:
                return PublishResult.failed(self.target, "account_lookup_failed", error)
            return await handler(client, content)
