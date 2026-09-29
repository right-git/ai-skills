---
name: icons
description: Use when a project needs a downloadable SVG icon from uxwing.com for a UI, website, animation, presentation, or video.
---

# UXWing SVG Icons

Use the bundled finder to discover UXWing assets, then download the selected
SVG URL unchanged. Search results are direct `.svg` URLs; do not rewrite file
extensions or guess download URLs.

## Find candidates

Run from the repository root. This project already has the required packages in
`.venv`:

```bash
.venv/bin/python .agents/skills/icons/scripts/find-icons.py \
  "microphone mute"
```

If the repository virtual environment is unavailable, run the script with
temporary dependencies instead of modifying the project:

```bash
uv run --with httpx --with beautifulsoup4 python \
  .agents/skills/icons/scripts/find-icons.py "microphone mute"
```

Try short English descriptions of the concept and state, such as `microphone
mute`, `mic off`, or `audio disabled`. Review several results and choose by
meaning, visual style, and compatibility with nearby icons. Visually inspect
the candidate URL when appearance matters; a filename alone is not sufficient.

## Download and verify

Use the exact URL printed by the finder. Download to a temporary file first so
an HTTP error or HTML response cannot replace a project asset:

```bash
icon_url='https://uxwing.com/wp-content/themes/uxwing/download/controller-and-music/mic-off-icon.svg'
icon_temp=$(mktemp "${TMPDIR:-/tmp}/uxwing-icon.XXXXXX")

curl --proto '=https' --fail --location --silent --show-error \
  "$icon_url" --output "$icon_temp"
test -s "$icon_temp"
xmllint --noout "$icon_temp"
rg -q '<svg([[:space:]>])' "$icon_temp"

mkdir -p public/icons
test ! -e public/icons/mic-muted.svg
install -m 0644 "$icon_temp" public/icons/mic-muted.svg
```

Adapt the destination to the project. Do not overwrite an existing asset unless
the request clearly calls for replacement. Inspect the installed SVG for the
expected `viewBox`, fills/strokes, and absence of scripts or unwanted external
references before embedding it.

## Quick reference

| Need | Action |
|---|---|
| No results | Broaden or simplify the English query |
| Too many results | Add the state or object, such as `off`, `outline`, or `arrow` |
| `ModuleNotFoundError` | Use `.venv/bin/python` or the `uv run --with ...` fallback |
| Confirm usage rights | Check the current [UXWing license](https://uxwing.com/license/) |

UXWing currently permits modification and personal, commercial, and client
use without attribution, but prohibits uses including logos/trademarks and
redistributing the source files as an icon collection. Follow the current
license page if its terms differ from this summary.

## Common mistakes

- Treating printed URLs as thumbnails and transforming them instead of using
  the direct SVG URLs.
- Choosing the first filename match without checking the icon visually.
- Renaming HTML, PNG, or an error response to `.svg` without XML validation.
