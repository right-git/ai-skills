"""YouTube client against a scripted Data API: token exchange, resumable
session, chunk resume after a dropped PUT, and the preflight rules."""

import asyncio
import json

import httpx

import api
from models import Content, Credentials, MediaItem, YouTubeAccount
from platforms import youtube

ACCOUNT = YouTubeAccount(
    name="chattler",
    platform="youtube",
    client_id="id.apps.googleusercontent.com",
    client_secret="shh",
    refresh_token="1//refresh",
)
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"


def _content(tmp_path, size, **extra):
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return Content(
        kind="video",
        captions={"youtube": "описание"},
        items=[MediaItem(path=path, kind="video", size=size)],
        title="Заголовок",
        **extra,
    )


class FakeYouTube:
    """Scripted server. `drop_first_chunk` makes the first data PUT fail once
    so the client must ask for the offset and resume."""

    def __init__(self, drop_first_chunk=False):
        self.drop_first_chunk = drop_first_chunk
        self.received = 0
        self.calls = []
        self.metadata = None
        self.dropped = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url.split("?")[0]))
        if url == youtube.TOKEN_URL:
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["grant_type"] == "refresh_token"
            assert body["refresh_token"] == "1//refresh"
            return httpx.Response(200, json={"access_token": "ya29.x"})
        assert request.headers["Authorization"] == "Bearer ya29.x"
        if request.method == "POST" and url.startswith(youtube.UPLOAD):
            self.metadata = json.loads(request.content)
            self.total = int(request.headers["X-Upload-Content-Length"])
            return httpx.Response(200, headers={"Location": SESSION})
        if request.method == "PUT" and url == SESSION:
            rng = request.headers["Content-Range"]
            if rng.startswith("bytes */"):
                return self._progress()
            start, _, rest = rng[len("bytes ") :].partition("-")
            end = int(rest.split("/")[0])
            if self.drop_first_chunk and not self.dropped:
                self.dropped = True
                raise httpx.ReadError("connection reset")
            assert int(start) == self.received, "chunk sent from the wrong offset"
            self.received = end + 1
            if self.received >= self.total:
                return httpx.Response(
                    200, json={"id": "vid123", "status": {"privacyStatus": "public"}}
                )
            return self._progress()
        raise AssertionError(f"unexpected request {request.method} {url}")

    def _progress(self):
        if self.received == 0:
            return httpx.Response(308)
        return httpx.Response(308, headers={"Range": f"bytes=0-{self.received - 1}"})


def _publish(server, content):
    client = youtube.YouTubeClient(
        ACCOUNT, transport=httpx.MockTransport(server.handler)
    )
    return asyncio.run(client.publish(content))


def test_uploads_in_chunks_and_returns_permalink(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "CHUNK", 1024)
    server = FakeYouTube()
    result = _publish(server, _content(tmp_path, 2500, tags=["a", "b"]))

    assert result.status == "published"
    assert result.id == "vid123"
    assert result.permalink == "https://youtu.be/vid123"
    assert result.detail is None
    assert server.metadata == {
        "snippet": {
            "title": "Заголовок",
            "description": "описание",
            "tags": ["a", "b"],
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    puts = [c for c in server.calls if c[0] == "PUT"]
    assert len(puts) == 3  # 1024 + 1024 + 452


def test_resumes_from_acknowledged_offset_after_dropped_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "CHUNK", 1024)
    server = FakeYouTube(drop_first_chunk=True)
    result = _publish(server, _content(tmp_path, 2048))

    assert result.status == "published"
    assert server.received == 2048
    # one dropped PUT, one status query, then the two real chunks
    puts = [c for c in server.calls if c[0] == "PUT"]
    assert len(puts) == 4


def test_reports_forced_private_and_auth_failure(tmp_path):
    def forced_private(request):
        if str(request.url) == youtube.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.x"})
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(
            200, json={"id": "v1", "status": {"privacyStatus": "private"}}
        )

    client = youtube.YouTubeClient(
        ACCOUNT, transport=httpx.MockTransport(forced_private)
    )
    result = asyncio.run(client.publish(_content(tmp_path, 10)))
    assert result.status == "published"
    assert "forced to private" in result.detail

    def bad_token(request):
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Token revoked"}
        )

    client = youtube.YouTubeClient(ACCOUNT, transport=httpx.MockTransport(bad_token))
    result = asyncio.run(client.publish(_content(tmp_path, 10)))
    assert result.status == "error"
    assert result.reason == "auth_failed"
    assert "invalid_grant" in result.detail
    assert "shh" not in result.detail


def test_preflight_requires_title_and_rejects_angle_brackets(tmp_path):
    creds = Credentials(accounts=[ACCOUNT])
    content = _content(tmp_path, 10)
    content.title = None
    problems = api.preflight([ACCOUNT], content, creds)
    assert any("--title is required" in p for p in problems)

    content.title = "<b>bold</b>"
    problems = api.preflight([ACCOUNT], content, creds)
    assert any("'<' or '>'" in p for p in problems)

    content.title = "ok"
    assert api.preflight([ACCOUNT], content, creds) == []

    content.kind = "photo"
    content.items[0].kind = "image"
    problems = api.preflight([ACCOUNT], content, creds)
    assert any("cannot publish photo" in p for p in problems)


def test_plan_shows_title_and_privacy(tmp_path):
    content = _content(tmp_path, 10, privacy="unlisted")
    [entry] = api.build_plan([ACCOUNT], content)
    assert entry.title == "Заголовок"
    assert entry.privacy == "unlisted"
    assert entry.upload == "resumable chunked upload"


def test_received_offset_parsing():
    assert youtube._received(None) == 0
    assert youtube._received("bytes=0-1048575") == 1048576
