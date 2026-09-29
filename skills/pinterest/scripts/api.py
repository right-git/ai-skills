#!/usr/bin/env python3
"""Pinterest pin search via py3-pinterest. Credentials come from the
environment or the repo-root .env (never hardcode them here)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from py3pin.Pinterest import Pinterest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = REPO_ROOT / ".env"


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def get_credentials() -> dict[str, str]:
    file_values = read_env_file()
    creds = {}
    for key in ("PINTEREST_EMAIL", "PINTEREST_PASSWORD", "PINTEREST_USERNAME"):
        value = os.environ.get(key) or file_values.get(key)
        if not value:
            raise SystemExit(
                f"{key} not set — copy {REPO_ROOT}/.env.example to .env "
                "and fill in your Pinterest credentials."
            )
        creds[key] = value
    return creds


def get_client() -> Pinterest:
    creds = get_credentials()
    pinterest = Pinterest(
        email=creds["PINTEREST_EMAIL"],
        password=creds["PINTEREST_PASSWORD"],
        username=creds["PINTEREST_USERNAME"],
        cred_root="cred_root",  # cookies stored here, created automatically
    )
    return pinterest


def search_pins(pinterest: Pinterest, query: str, max_results: int = 10) -> list[str]:
    results = []
    search_batch = pinterest.search(
        scope="pins", reset_bookmark=True, query=query, page_size=max_results
    )
    for pin in search_batch:
        results.append(pin["images"]["orig"]["url"])
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search Pinterest pins by query.")
    parser.add_argument("query", help="search text")
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--out", default="pins.json", help="output JSON path")
    args = parser.parse_args()

    client = get_client()
    pins = search_pins(client, args.query, args.max_results)
    print(len(pins))
    print(pins)
    Path(args.out).write_text(json.dumps(pins, indent=4), encoding="utf-8")
