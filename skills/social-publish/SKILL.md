---
name: social-publish
description: Use when publishing a finished video, carousel, photo or text post to Instagram, Threads, X (Twitter), Telegram or YouTube through their official APIs, with token-only credentials for multiple accounts (YouTube needs a one-time `youtube-auth` consent).
---

# Social publish

Publishes one post to one or more social accounts from local files in
`videos/` or `assets/`. Official APIs only, token authentication only — no
OAuth browser flow at publish time. The one exception is YouTube, whose API
has no long-lived tokens you can paste: `publish.py youtube-auth` opens the
browser **once** to obtain a refresh token, and every publish after that is
token-only.

## Setup

`.env` at the repo root holds only the path to the credentials:

```
SOCIAL_CREDENTIALS_PATH="/absolute/path/to/social-credentials.json"
```

Copy `social-credentials.example.json` from this skill folder as a starting
point. Keep the real file **outside the repo** and `chmod 600` it — it is
rewritten in place when Meta tokens are refreshed, and it holds every secret.

```json
{
  "media_host": {
    "endpoint": "https://<account>.r2.cloudflarestorage.com",
    "bucket": "video-gen-public",
    "public_base": "https://cdn.example.com",
    "access_key_id": "...",
    "secret_access_key": "...",
    "region": "auto"
  },
  "accounts": [
    {"name": "chattler", "platform": "instagram",
     "access_token": "IGAA..."},
    {"name": "chattler", "platform": "threads",
     "access_token": "THQW..."},
    {"name": "chattler", "platform": "x",
     "consumer_key": "...", "consumer_secret": "...",
     "access_token": "...", "access_token_secret": "..."},
    {"name": "chattler", "platform": "telegram",
     "bot_token": "7712:AAF...", "chat_id": "@chattler"},
    {"name": "chattler", "platform": "youtube",
     "client_id": "1077...apps.googleusercontent.com",
     "client_secret": "GOCSPX-...", "refresh_token": "1//0g..."}
  ]
}
```

`name` groups handles that belong together; `(name, platform)` must be unique,
so two Telegram channels need two names. Unknown keys are preserved on rewrite,
so notes and labels are safe to add.

Every field above is required and is the whole of what you must supply.
Instagram and Threads take **only the access token** — the account id is
resolved from the token on first use, and `expires_at` is written into the file
by the refresh that renews the token. Both may be pinned by hand (`user_id`) if
you ever need to, but never have to be.

**Where each credential comes from**

| platform | credential | how |
|---|---|---|
| instagram | Instagram user access token | Business/Creator account + Meta app with *Instagram API with Instagram Login*. Expires in 60 days, auto-refreshed. |
| threads | Threads user access token | Meta app with the Threads API. Expires in 60 days, auto-refreshed. |
| x | consumer key/secret + access token/secret | X developer app, OAuth 1.0a user context, **Read and write** permission. Never expires. |
| telegram | bot token + chat id | @BotFather; add the bot as an administrator of the channel. Never expires. |
| youtube | OAuth client id/secret + refresh token | Written by `publish.py youtube-auth` from a Google Cloud **Desktop app** client JSON (see below). Never expires while the consent screen is *In production*. |

**No App Review or Business Verification is needed** for the accounts you own.
Meta's Standard Access is "intended for apps that will only be used by people
who have roles on them", and it covers content publishing. Verification is a
gate on *Advanced* Access, which you only need to serve accounts you do not
manage. So: keep the app in **Development mode**, add the Instagram/Threads
account under *App roles → Instagram testers*, accept the invite from the
account itself (Instagram → Settings → Apps and websites → Tester invites),
then generate the token in the dashboard. Do not switch the app to Live — that
is what pushes you into the review path.

Confirm publishing permission is live before trusting it, without posting
anything:

```bash
curl -s "https://graph.instagram.com/v23.0/me/content_publishing_limit\
?fields=quota_usage,config&access_token=$TOKEN"
```

A JSON quota back means the publish permission works; an OAuth error means the
token is missing `instagram_business_content_publish`.

**YouTube setup** (once per Google account):

1. Google Cloud → the project → *APIs & Services → Library* → enable
   **YouTube Data API v3**.
2. *OAuth consent screen*: External, add the scopes
   `youtube.upload` and `youtube.readonly`, then **Publish app** (status
   *In production*). No verification is needed for your own channel — Google
   shows an "unverified app" warning you click through. Leaving it in
   *Testing* makes every refresh token die after 7 days.
3. *Credentials → Create credentials → OAuth client ID → Desktop app*,
   download the JSON.
4. Run the consent flow; it imports the client JSON into the credentials file
   and stores the refresh token, so the downloaded file can be deleted:

```bash
.venv/bin/python .agents/skills/social-publish/scripts/publish.py youtube-auth \
  --account chattler --client-secret ~/Downloads/client_secret_*.json
```

The browser opens on the channel picker; choose the channel that should own
the uploads. `--no-browser` prints the URL instead. The result echoes
`channel_title` so you can confirm the right channel was picked.

Two YouTube facts to plan around: an API project that has not passed
Google's compliance audit uploads **everything as private** regardless of
`--privacy` (the result's `detail` says so; flip it in YouTube Studio), and
the default quota of 10 000 units/day allows about **six uploads per day**
(`videos.insert` costs 1 600). A 9:16 video up to 3 minutes is automatically
a Short; no `#Shorts` tag is needed.

`media_host` is any S3-compatible bucket (R2, S3, MinIO, B2, Wasabi) whose
objects are publicly readable at `public_base`. It is **required** for Threads
and for Instagram photos/carousels, because those APIs fetch media themselves
rather than accepting an upload. Objects are deleted once the run finishes.

## Limits — check before composing, do not attempt what is not supported

| | instagram | threads | x | telegram | youtube |
|---|---|---|---|---|---|
| **text** | ✗ not possible | ✓ | ✓ | ✓ | ✗ |
| **photo** | ✓ 1 image | ✓ 1 image | ✓ up to 4 images | ✓ 1 image | ✗ |
| **video** | ✓ Reel | ✓ | ✓ 1 video, never mixed with images | ✓ | ✓ needs `--title` |
| **carousel** | ✓ 2–10 | ✓ 2–20 | ✗ **no carousel at all** | ✓ 2–10 | ✗ |
| **caption** | 2200 | 500 | 280 | 1024 | 5000 description, 100 title |
| **file size** | — | — | 5 MB image / 512 MB video | 10 MB image / 50 MB video | 256 GB |

Consequences to plan around rather than discover:

- **Instagram cannot receive a text-only post.** Never include it in a text run.
- **X has no carousel.** More than one image is a `photo` post with up to 4
  files; more than four, or any video alongside images, is impossible.
- **A caption written for a Reel will not fit X.** Write `--caption-x`
  (or `--text-x`) up front rather than letting pre-flight reject the run.
- Instagram Reels are eligible for the Reels tab only at 9:16 and 5–90 s.
- **YouTube is video-only and needs `--title`** (≤100 chars). The caption
  becomes the description; neither may contain `<` or `>`. `--tags a,b` and
  `--privacy public|unlisted|private` are YouTube-only; `--cover` doubles as
  the thumbnail (needs a phone-verified channel, otherwise it is a warning).
  `--tags` must fill most of YouTube's 500-character limit (aim 350–480):
  a handful of tags (~100 chars) leaves the tag field visibly empty in
  Studio. Build the set from channel name + topic in the video's language +
  the same topic in English + format words + every named subject (film,
  person, place) in both languages; measure with
  `printf '%s' "$TAGS" | wc -m` before the dry run. `publish.py` cannot
  retag a published video — a thin tag set is fixable only by hand in
  YouTube Studio.
- A named platform that cannot carry the content aborts the whole run and
  publishes nothing, including under `--platform all`.

## Publishing

Always dry-run first, show the plan, get the user's approval, then re-run with
`--confirm`. Without `--confirm` nothing touches the network.

```bash
.venv/bin/python .agents/skills/social-publish/scripts/publish.py video \
  --account chattler --platform instagram,threads,telegram,youtube \
  --file videos/20260909-kv-cache/kv-cache.mp4 \
  --caption "Длинный текст для Reels…" \
  --caption-x "Короткий вариант" \
  --title "KV-кэш за 60 секунд" --tags "ai,llm"
```

```bash
publish.py accounts                       # what is configured
publish.py text     --account A --platform threads,x --text "…"
publish.py photo    --account A --platform x --file a.jpg --file b.jpg
publish.py video    --account A --platform instagram --file v.mp4 [--cover c.jpg]
publish.py carousel --account A --platform instagram,threads --file a.jpg --file b.jpg
publish.py refresh                        # force-renew Meta tokens
publish.py youtube-auth --account A --client-secret client_secret.json  # once
```

`--platform` is required; `all` must be spelled out. Other flags:
`--chat-id` overrides the Telegram channel, `--creds` overrides the env path,
`--cover` sets a Reel cover (Instagram) or thumbnail (YouTube); `--title`,
`--tags`, `--privacy` apply to YouTube only.

Exit codes: `0` published or dry-run, `1` a target failed, `2` pre-flight or
configuration error. Output is JSON on stdout (failures on stderr) with one
result per target, carrying `id` and `permalink`. Report the permalinks back to
the user.

## Rate limits

X Free tier allows 500 posts/month and only 17 media `initialize`/`finalize`
calls per 24 h — budget video posts. Instagram allows 50 published posts per
24 h, Threads 250, YouTube about 6 (10 000 quota units, 1 600 per upload).
A carousel counts as one post everywhere.

## Common mistakes

- Publishing without showing the dry-run plan first. Posts cannot be edited on
  Instagram or Threads, and only the bot's own Telegram messages are deletable.
- Passing a `--platform` the account has no entry for, instead of running
  `publish.py accounts` first.
- Trying to reach Threads without `media_host` configured — Threads accepts no
  file uploads whatsoever, only URLs it can fetch.
- Putting credentials in `.env` itself. `.env` holds the *path*; the secrets
  live in the JSON file it points at.
- Passing several `--file` to `photo` for a non-X platform. Two or more images
  is a `carousel` everywhere except X.
- Sending a video to YouTube without `--title`, or with a caption written for
  Reels that carries `<`/`>` — pre-flight rejects the whole run.
- Expecting a public YouTube video from a project that has not passed the
  API audit: it lands as private, and the result says so.

## Verification and lint

```bash
.venv/bin/python -m pytest .agents/skills/social-publish/tests -q
.venv/bin/black .agents/skills/social-publish/
.venv/bin/flake8 .agents/skills/social-publish/
```

The tests pin AWS SigV4 and X's OAuth 1.0a signatures to published vectors;
those two fail live as bare 401/403s with no indication of the cause. The
YouTube tests drive the client against a scripted resumable-upload server,
including a dropped chunk that must resume from the acknowledged offset.
