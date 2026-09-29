---
name: clip-cafe
description: Use when movie or TV footage must be found by spoken quote, scene phrase, title, actor, character, director, writer, genre, rating, language, year, or decade through Clip.Cafe.
---

# Find Clip.Cafe Footage

Use the bundled finder to search Clip.Cafe, locate the words inside each clip,
and optionally save one reviewed result. Search before downloading: the first
match is not automatically the best editorial choice.

## Search

Run from the repository root:

```bash
.venv/bin/python .agents/skills/clip.cafe/scripts/find-clips.py \
  "oh my god" --exact --title "Spider Man" --limit 5 --json
```

For a broad query, only the phrase is required:

```bash
.venv/bin/python .agents/skills/clip.cafe/scripts/find-clips.py "we need to talk"
```

Run `--help` for all filters. They include exact match, title, actor,
character, director, writer, movies versus TV, rating, genre, interface/search
language, audio language, subtitle language, year range, duration, sort, and
decade. `--decade 80s` means 1980–1989.

If `.venv` is unavailable, use temporary dependencies without changing the
project:

```bash
uv run --with httpx --with beautifulsoup4 python \
  .agents/skills/clip.cafe/scripts/find-clips.py "oh my god" --json
```

The finder tries Clip.Cafe's undocumented same-origin JSON endpoint, then
automatically parses the public results page if that response is unavailable
or incompatible. It does not treat a valid empty result as an API failure.

## Read the result correctly

- `match_start_sec` and `match_end_sec` locate the phrase **inside this clip**.
- `movie_timestamp` is where the **clip begins** in the source movie or episode.
  `source_match_start` and `source_match_end` add the clip-local offsets and
  give the phrase's approximate position in the source.
- `page_url` is for reviewing the candidate; `mp4_url` and `hls_url` are media.
- `enrichment_errors` explains missing optional timing or detail fields.

Inspect several candidates for the actual line, title/year, character, visual
context, exactness, quality, and languages. Open `page_url` when appearance or
context matters. `--exact` requests an exact phrase, but still check each
result's `exact` value; unknown or relaxed matches may appear. Title matching
can span a franchise, so add year bounds when one specific film is required.
Never construct media URLs from a slug.

## Download one reviewed result

Use the reviewed result's stable `slug` with the same query and filters:

```bash
.venv/bin/python .agents/skills/clip.cafe/scripts/find-clips.py \
  "oh my god" --exact --title "Spider Man" --limit 5 \
  --slug "oh-my-god-s873" --download assets/movie-clips/reaction.mp4 --quiet
```

Prefer `--slug` after a JSON review because it remains stable if rankings move.
`--pick N` is available for quick one-based selection, but it applies to the
freshly executed result list and is not a persistent bookmark.
For a reviewed download, use `--quiet`: the finder enriches only the selected
result and avoids printing the full candidate document, reducing requests,
latency, and tool-output tokens.

Nothing downloads without `--download PATH`. The command refuses to overwrite
an existing file; use `--force` only when replacement of that exact path was
requested. It accepts only an HTTPS Clip.Cafe MP4 exposed by the search or clip
page, validates the response as nonempty video data, and installs it atomically.

The direct download sends the reviewed clip page as `Referer`. Keep this direct
path as the default: Clip.Cafe's public JSON/detail page normally exposes the
same MP4 URL that the browser player requests, so launching Chromium would only
add latency and dependencies.

## Capture the public player traffic

If a reviewed public clip plays in the browser but its exposed MP4 is missing or
the direct request fails, retry that selected result once with the browser
fallback:

```bash
.venv/bin/python .agents/skills/clip.cafe/scripts/find-clips.py \
  "oh my god" --exact --slug "oh-my-god-s873" \
  --download assets/movie-clips/reaction.mp4 --browser-fallback --quiet
```

The fallback starts a fresh ephemeral Playwright Chromium context, clicks the
public player, and watches responses for an HTTPS MP4 or HLS playlist. It exits
as soon as MP4 is confirmed (or after a one-second HLS grace period), reuses only
the observed `User-Agent`, `Referer`, `Origin`, `Accept`, and `Accept-Language`
headers, and deliberately drops cookies, authorization, and partial `Range`
headers. MP4 is streamed with httpx; HLS is remuxed to MP4 with local ffmpeg.

For network diagnostics or a known clip page, call the capture tool directly:

```bash
.venv/bin/python .agents/skills/clip.cafe/scripts/capture-media.py \
  "https://clip.cafe/movie-slug/clip-slug/" /tmp/clip.mp4 \
  --network-log /tmp/clip-network.json --json
```

Install the skill requirements and Chromium once when this fallback is needed:

```bash
uv pip install --python .venv/bin/python -r .agents/skills/clip.cafe/scripts/requirements.txt
.venv/bin/python -m playwright install chromium
```

Do not add `--browser-fallback` preemptively. It is slower than direct download
and is only useful when the public player's actual request must be observed.
The capture tool accepts only public HTTPS `clip.cafe` page URLs and never loads
a persistent browser profile.

On HTTP 429, stop and report the retry interval. If both the direct request and
fresh public-player capture fail, stop and report the error. Do not loop,
automate login, reuse account cookies, use API keys, defeat DRM, bypass
subscriptions, or imitate paid download operations.

Technical access is not permission to reuse footage. Confirm the intended use
is authorized or otherwise lawful, and follow Clip.Cafe's current
[terms](https://clip.cafe/termsandconditions/) and
[DMCA guidance](https://clip.cafe/dmca/).

## Common mistakes

- Confusing phrase seconds inside the clip with the source movie timestamp.
- Downloading rank 1 without reviewing alternatives.
- Adding `--force` merely to make a failed command succeed.
- Treating the undocumented JSON response as a stable public API.
