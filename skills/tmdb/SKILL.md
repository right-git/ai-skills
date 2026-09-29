---
name: tmdb
description: Use when an official movie or TV poster, title, year, or backdrop is needed by exact film and year through TMDB (The Movie Database).
---

# Find TMDB Posters

Use the bundled finder to resolve an exact film (title + year) to its TMDB
record, list official posters by community votes, and optionally save one
reviewed result. Search before downloading: the top-voted poster is not
automatically the best editorial choice (teasers, textless variants and
re-release art all compete).

## Setup

Requires `TMDB_API_KEY` in the repo-root `.env` (copy `.env.example` if
missing): the free **API Read Access Token** (v4 Bearer) from
themoviedb.org → Settings → API. Without it the finder stops with a clear
error instead of guessing — treat that as `BLOCKED`, not as a cue to scrape.

## Search

Run from the repository root:

```bash
.venv/bin/python .agents/skills/tmdb/scripts/tmdb.py \
  --title "Fight Club" --year 1999 --json
```

Resolves the exact film (verifies title/year against the record, refuses
ambiguous matches unless `--pick N` selects one) and lists poster
candidates sorted by votes, with dimensions, language and thumbnail URLs.

For TV use `--tv "Breaking Bad"` instead of `--title/--year`.

Run `--help` for all options: `--lang` (metadata language, default `ru-RU`
with `en-US` fallback names), `--limit`, `--min-width`, `--no-textless`
(reject posters without title text), `--pick N`, `--file-path PATH`
(stable TMDB file path from a previous `--json` review).

## Read the result correctly

- `tmdb_id`, `title`, `original_title`, `year` identify the verified record —
  never download for an unverified one.
- `file_path` (e.g. `/pB8BM7pdSp6B6Ih7QZ4DrQ3Pm.jpg`) is a stable bookmark;
  `--file-path` reuses it without re-running the search ranking.
- `vote_average`/`vote_count` rank community preference, not correctness:
  still open the `preview_url` and check title spelling, year billing and
  that the billed cast matches the film.
- Poster files come from TMDB's documented CDN
  (`https://image.tmdb.org/t/p/original` + `file_path`); `--size w500`
  fetches a smaller rendition of the same file for drafts.

## Download one reviewed result

```bash
.venv/bin/python .agents/skills/tmdb/scripts/tmdb.py \
  --title "Fight Club" --year 1999 --pick 1 \
  --download projects/remotion/<slug>/media/assets/fight-club/poster.jpg --quiet
```

Nothing downloads without `--download PATH`. The command refuses to
overwrite an existing file; use `--force` only when replacement of that
exact path was requested. It validates the response as nonempty JPEG/PNG
data (magic bytes + minimum size) and installs it atomically.

Availability on TMDB is not permission to reuse the artwork. Record the
`page_url` (`https://www.themoviedb.org/movie/<id>`) and the chosen
`file_path` in the project's assets manifest.

## Common mistakes

- Downloading rank 1 without opening previews (wrong-language billing,
  textless teaser, anniversary re-release).
- Matching by title alone across remakes — `--year` is required for movies.
- Committing `TMDB_API_KEY` into scripts or tracked files instead of `.env`.
- Treating vote rank as proof of correctness; votes measure taste.

Base directory for this skill: /Users/a1/Desktop/Personal/video-gen/.agents/skills/tmdb
Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.
Note: file list is sampled.

<skill_files>
</skill_files>
