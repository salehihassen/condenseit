"""Hacker News collector via the official public Firebase JSON API.

No authentication or API key is required. Rate limiting is handled by
fetching only the item detail pages we intend to use.
"""

import logging
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from condenseit.collectors.article_text import fetch_article_text
from condenseit.collectors.feed_dates import unix_timestamp_to_iso
from condenseit.collectors.health import collect_with_health
from condenseit.config import HackerNewsConfig
from condenseit.fetch_headers import digest_fetch_headers
from condenseit.store.database import ContentStore

logger = logging.getLogger(__name__)

_HN_BASE = "https://hacker-news.firebaseio.com/v0"
_HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
_VALID_FEEDS = frozenset({"top", "best", "new", "ask", "show"})
_ARTICLE_CHARS = 1800
_COMMENT_CHARS = 260
_MAX_COMMENTS = 6
_MAX_COMMENT_IDS = 12


class _CommentText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(markup: str) -> str:
    parser = _CommentText()
    parser.feed(markup)
    return " ".join("".join(parser.parts).split())


class HackerNewsCollector:
    """Collect top/best/new/ask/show stories from Hacker News."""

    def __init__(self, sources: list[HackerNewsConfig]) -> None:
        self.sources = sources
        self._client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers=digest_fetch_headers(),
        )

    def collect_all_with_health(
        self,
    ) -> tuple[list[dict[str, str]], list[tuple[str, str | None, int]]]:
        """Return ``(articles, [(feed_url, error_or_none, item_count), ...])``."""
        articles: list[dict[str, str]] = []
        health: list[tuple[str, str | None, int]] = []
        for cfg in self.sources:
            feed = cfg.feed if cfg.feed in _VALID_FEEDS else "top"
            feed_url = f"{_HN_BASE}/{feed}stories.json"
            items, entry = collect_with_health(
                feed_url,
                lambda cfg=cfg, feed=feed, feed_url=feed_url: self._collect_feed(
                    cfg, feed, feed_url
                ),
                log_label=f"HN collect failed for feed {feed!r}",
            )
            articles.extend(items)
            health.append(entry)
        return articles, health

    def _collect_feed(
        self,
        cfg: HackerNewsConfig,
        feed: str,
        feed_url: str,
    ) -> list[dict[str, str]]:
        resp = self._client.get(feed_url)
        resp.raise_for_status()
        story_ids: list[int] = resp.json() or []

        items: list[dict[str, str]] = []
        checked = 0

        for story_id in story_ids:
            if len(items) >= cfg.max_items:
                break
            # Fetch enough candidate items to satisfy max_items even after
            # score filtering; bail early if we checked 3x max_items.
            if checked >= cfg.max_items * 3:
                break
            checked += 1

            try:
                item = self._fetch_item(story_id)
            except Exception:
                logger.debug("HN item fetch failed for %d", story_id, exc_info=True)
                continue

            if not item:
                continue
            score = int(item.get("score") or 0)
            if score < cfg.min_score:
                continue
            url = item.get("url", "")
            title = item.get("title", "")
            if not title:
                continue

            if url:
                article = self._extract_content(url) or ""
            else:
                # Self/Ask post - use its text before the discussion.
                article = _plain_text(item.get("text") or "") or title

            comments = self._fetch_comments(item)
            if not article.strip() and not comments:
                continue

            published = self._ts_to_iso(item.get("time"))
            hn_link = _HN_ITEM_URL.format(id=story_id)
            sections = [
                "Hacker News story. Summarize the article and discussion; "
                "distinguish article claims from commenter opinions."
            ]
            if url:
                sections.append(f"Original article: {url}")
            if article:
                sections.append(f"Article or post excerpt:\n{article[:_ARTICLE_CHARS]}")
            if comments:
                total = item.get("descendants") or len(comments)
                sections.append(
                    f"Hacker News comments (sample of {len(comments)} from "
                    f"{total} comments):\n" + "\n".join(comments)
                )
            content = "\n\n".join(sections)[:4000]
            items.append(
                {
                    "url": url or hn_link,
                    "title": title,
                    "content": content,
                    "source": f"Hacker News ({feed})",
                    "category": cfg.category,
                    "content_hash": ContentStore.content_hash(content),
                    "published_at": published,
                    "collected_at": datetime.now(UTC).isoformat(),
                },
            )
        return items

    def _fetch_item(self, story_id: int) -> dict[str, Any] | None:
        resp = self._client.get(f"{_HN_BASE}/item/{story_id}.json")
        resp.raise_for_status()
        return resp.json()

    def _extract_content(self, url: str) -> str:
        return fetch_article_text(self._client, url) or ""

    def _fetch_comments(self, story: dict[str, Any]) -> list[str]:
        comments: list[str] = []
        for comment_id in (story.get("kids") or [])[:_MAX_COMMENT_IDS]:
            if len(comments) >= _MAX_COMMENTS:
                break
            try:
                comment = self._fetch_item(comment_id)
            except Exception:
                logger.debug(
                    "HN comment fetch failed for %s", comment_id, exc_info=True
                )
                continue
            if not comment or comment.get("deleted") or comment.get("dead"):
                continue
            body = _plain_text(comment.get("text") or "")[:_COMMENT_CHARS]
            if body:
                comments.append(f"- {comment.get('by') or 'anonymous'}: {body}")
        return comments

    @staticmethod
    def _ts_to_iso(ts: Any) -> str:
        return unix_timestamp_to_iso(ts)
