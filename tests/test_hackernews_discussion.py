"""HN digest cards should lead to, and summarize, the discussion."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from condenseit.collectors.hackernews import HackerNewsCollector
from condenseit.config import HackerNewsConfig
from condenseit.store.database import ContentStore
from condenseit.web.app import _build_digest_detail


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Client:
    def __init__(self, items):
        self.items = items

    def get(self, url):
        if url.endswith("/topstories.json"):
            return _Response([42])
        return _Response(self.items[int(url.split("/")[-1].split(".")[0])])


class HackerNewsDiscussionTest(unittest.TestCase):
    def test_article_and_comments_reach_summary_and_card_links_to_hn(self):
        story = {
            "id": 42,
            "title": "A useful release",
            "score": 100,
            "url": "https://example.com/release",
            "kids": [51, 52],
            "descendants": 2,
            "time": 1_700_000_000,
        }
        collector = HackerNewsCollector([HackerNewsConfig(feed="top", min_score=1)])
        collector._client = _Client(
            {
                42: story,
                51: {"by": "alice", "text": "<p>It works, but setup is slow.</p>"},
                52: {"by": "bob", "text": "<p>Try the new installer.</p>"},
            }
        )
        collector._extract_content = lambda url: "Release details: faster builds. " * 100

        articles = collector._collect_feed(
            collector.sources[0],
            "top",
            "https://hacker-news.firebaseio.com/v0/topstories.json",
        )

        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["url"], "https://news.ycombinator.com/item?id=42")
        self.assertEqual(article["discussion_url"], article["url"])
        self.assertEqual(article["original_url"], "https://example.com/release")
        self.assertIn("Original article: https://example.com/release", article["content"])
        self.assertIn("Release details: faster builds", article["content"])
        self.assertIn("alice: It works, but setup is slow.", article["content"])
        self.assertIn("bob: Try the new installer.", article["content"])
        self.assertLessEqual(len(article["content"]), 4000)

    def test_comments_keep_story_when_external_article_cannot_be_fetched(self):
        story = {
            "id": 42,
            "title": "An inaccessible article",
            "score": 100,
            "url": "https://example.com/blocked",
            "kids": [51],
            "time": 1_700_000_000,
        }
        collector = HackerNewsCollector([HackerNewsConfig(feed="top", min_score=1)])
        collector._client = _Client(
            {42: story, 51: {"by": "alice", "text": "<p>Here is the gist.</p>"}}
        )
        collector._extract_content = lambda url: ""

        articles = collector._collect_feed(
            collector.sources[0],
            "top",
            "https://hacker-news.firebaseio.com/v0/topstories.json",
        )

        self.assertEqual(len(articles), 1)
        self.assertIn("alice: Here is the gist.", articles[0]["content"])

    def test_discussion_link_survives_same_day_storage(self):
        with TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "test.db")
            article = {
                "url": "https://news.ycombinator.com/item?id=42",
                "discussion_url": "https://news.ycombinator.com/item?id=42",
                "original_url": "https://example.com/release",
                "title": "A useful release",
                "content": "Release details and discussion",
                "source": "Hacker News (top)",
                "category": "General",
                "content_hash": ContentStore.content_hash("Release details and discussion"),
                "published_at": "2026-09-28T10:00:00+00:00",
                "collected_at": "2026-09-28T10:01:00+00:00",
            }
            store.save_article(article)
            rows = store.articles_collected_since(datetime(2026, 9, 28, tzinfo=UTC))
            self.assertEqual(rows[0]["discussion_url"], article["discussion_url"])
            self.assertEqual(rows[0]["original_url"], article["original_url"])
            store.close()

    def test_saved_digest_receives_discussion_link_from_article_metadata(self):
        with TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "test.db")
            original = "https://example.com/release"
            discussion = "https://news.ycombinator.com/item?id=42"
            store.save_article(
                {
                    "url": original,
                    "title": "A useful release",
                    "content": "Release details",
                    "source": "Hacker News (top)",
                    "category": "General",
                    "content_hash": ContentStore.content_hash("Release details"),
                    "published_at": "2026-09-28T10:00:00+00:00",
                    "collected_at": "2026-09-28T10:01:00+00:00",
                    "discussion_url": discussion,
                    "original_url": original,
                }
            )
            saved_digest = {
                "id": 1,
                "created_at": "2026-09-28T12:00:00+00:00",
                "markdown": "",
                "stats_json": json.dumps(
                    {"digest_items": [{"url": original, "title": "A useful release",
                                        "source": "Hacker News (top)", "summary": "Summary"}]}
                ),
            }
            item = _build_digest_detail(saved_digest, store)["items"][0]
            self.assertEqual(item["discussion_url"], discussion)
            self.assertEqual(item["original_url"], original)
            store.close()


if __name__ == "__main__":
    unittest.main()
