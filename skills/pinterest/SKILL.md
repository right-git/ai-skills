---
name: pinterest
description: Use when a video project needs reference images sourced from Pinterest by search query, for mood boards, style extraction, or visual research.
---

# Pinterest

Search Pinterest pins by query and collect their original image URLs, for
gathering visual references before `extracting-design-styles` or asset
sourcing.

## Setup

Requires `PINTEREST_EMAIL`, `PINTEREST_PASSWORD`, and `PINTEREST_USERNAME` in
the repo-root `.env` (copy `.env.example` if missing). Never hardcode
credentials in `scripts/api.py` or any committed file.

```bash
uv run --no-project --with-requirements .agents/skills/pinterest/scripts/requirements.txt \
  python .agents/skills/pinterest/scripts/api.py "query text" --max-results 20 --out pins.json
```

Writes `pins.json` (a list of original-resolution image URLs) to the given
`--out` path and prints the pin count.

## Use in a project

Treat the resulting URLs as candidates, not final assets: download only the
images the user selects, and follow the same rules as any other reference —
never claim exact typefaces or colors from a screenshot alone, and route
anything reusable through `extracting-design-styles` rather than copying
Pinterest content directly into a video.

## Common mistakes

- Committing real credentials into `api.py` instead of `.env`.
- Downloading and using pins without checking usage rights for the final video.
- Treating raw search results as approved references without user selection.
