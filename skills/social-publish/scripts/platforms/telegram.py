"""Telegram publishing over the Bot API.

Everything is uploaded as multipart directly from disk, so the media host is
never used. The bot must be an administrator of the target channel.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from models import Content, PublishResult, TelegramAccount
from utils import content_type, send

API = "Telegram API"
BASE = "https://api.telegram.org"


class TelegramClient:
    def __init__(
        self,
        account: TelegramAccount,
        media: Any = None,
        *,
        chat_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.account = account
        self.chat_id = chat_id or account.chat_id
        self.transport = transport
        self.timeout = timeout
        self.target = account.target
        self.secrets = (account.bot_token,)

    async def _send(self, client: httpx.AsyncClient, method: str, **kwargs: Any):
        return await send(
            client,
            "POST",
            f"{BASE}/bot{self.account.bot_token}/{method}",
            api=API,
            secrets=self.secrets,
            **kwargs,
        )

    def _permalink(self, message_id: str) -> str | None:
        chat = self.chat_id
        if chat.startswith("@"):
            return f"https://t.me/{chat[1:]}/{message_id}"
        if chat.startswith("-100"):
            return f"https://t.me/c/{chat[4:]}/{message_id}"
        return None

    def _file(self, item) -> tuple[str, bytes, str]:
        return (item.path.name, item.path.read_bytes(), content_type(item.path))

    async def publish(self, content: Content) -> PublishResult:
        caption = content.caption("telegram")
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            if content.kind == "text":
                payload, error = await self._send(
                    client,
                    "sendMessage",
                    data={"chat_id": self.chat_id, "text": caption},
                )
            elif content.kind == "carousel":
                group = []
                files = {}
                for index, item in enumerate(content.items):
                    field = f"file{index}"
                    entry: dict[str, Any] = {
                        "type": "video" if item.kind == "video" else "photo",
                        "media": f"attach://{field}",
                    }
                    if index == 0 and caption:
                        entry["caption"] = caption
                    group.append(entry)
                    files[field] = self._file(item)
                payload, error = await self._send(
                    client,
                    "sendMediaGroup",
                    data={
                        "chat_id": self.chat_id,
                        "media": json.dumps(group, ensure_ascii=False),
                    },
                    files=files,
                )
            else:
                item = content.items[0]
                method = "sendVideo" if item.kind == "video" else "sendPhoto"
                field = "video" if item.kind == "video" else "photo"
                data = {"chat_id": self.chat_id, "caption": caption}
                if item.kind == "video":
                    data["supports_streaming"] = "true"
                payload, error = await self._send(
                    client, method, data=data, files={field: self._file(item)}
                )

        if payload is None:
            return PublishResult.failed(self.target, "publish_failed", error)
        result = payload.get("result")
        if isinstance(result, list):
            result = result[0] if result else {}
        message_id = str((result or {}).get("message_id") or "")
        if not message_id:
            return PublishResult.failed(
                self.target, "publish_failed", "response contains no message_id"
            )
        return PublishResult(
            target=self.target,
            status="published",
            id=message_id,
            permalink=self._permalink(message_id),
        )
