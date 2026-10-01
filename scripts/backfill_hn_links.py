"""Recover HN discussion links for articles collected before link metadata existed.

Dry run by default. Use --apply after reviewing the match count. Only rows whose
stored source is Hacker News are considered, and external URLs must match the
HN story's URL exactly before a discussion link is written.
"""

import argparse
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


HN_PREFIX = "https://news.ycombinator.com/item?id="
ORIGINAL_RE = re.compile(r"^Original article: (https?://\S+)", re.MULTILINE)


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "condenseit-c3/1"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def _resolve(title: str, original_url: str, published_at: str) -> str:
    query = urllib.parse.urlencode(
        {"query": title, "tags": "story", "hitsPerPage": "100"}
    )
    hits = _get_json(f"https://hn.algolia.com/api/v1/search_by_date?{query}").get(
        "hits", []
    )
    exact = [
        hit for hit in hits
        if str(hit.get("url") or "").rstrip("/") == original_url.rstrip("/")
    ]
    if not exact:
        return ""
    try:
        published = datetime.fromisoformat(published_at).astimezone(UTC).timestamp()
        exact.sort(key=lambda hit: abs(float(hit.get("created_at_i") or 0) - published))
    except (ValueError, TypeError):
        pass
    for hit in exact:
        story_id = str(hit.get("objectID") or "")
        if not story_id.isdigit():
            continue
        item = _get_json(
            f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
        )
        if str(item.get("url") or "").rstrip("/") == original_url.rstrip("/"):
            return HN_PREFIX + story_id
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db, timeout=30)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT url, title, content, published_at FROM articles "
        "WHERE source LIKE 'Hacker News%'"
    ).fetchall()
    updates: list[tuple[str, str, str]] = []
    unresolved: list[str] = []
    for row in rows:
        url = str(row["url"])
        if url.startswith(HN_PREFIX):
            discussion_url = url
            match = ORIGINAL_RE.search(str(row["content"] or ""))
            original_url = match.group(1) if match else ""
        else:
            original_url = url
            try:
                discussion_url = _resolve(
                    str(row["title"]), url, str(row["published_at"] or "")
                )
            except (OSError, ValueError, TypeError):
                discussion_url = ""
        if discussion_url:
            updates.append((discussion_url, original_url, url))
        else:
            unresolved.append(url)

    print(f"HN articles: {len(rows)}; matched: {len(updates)}; unresolved: {len(unresolved)}")
    for url in unresolved:
        print(f"unresolved: {url}")
    if args.apply:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(articles)")}
        if not {"discussion_url", "original_url"}.issubset(columns):
            raise SystemExit("Run the upgraded CondenseIt once to migrate the schema")
        with connection:
            connection.executemany(
                "UPDATE articles SET discussion_url = ?, original_url = ? WHERE url = ?",
                updates,
            )
        print(f"updated: {len(updates)}")
    connection.close()


if __name__ == "__main__":
    main()
