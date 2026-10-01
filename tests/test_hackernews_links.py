"""HN items expose article and discussion links without changing URL identity."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from condenseit.collectors.hackernews import HackerNewsCollector
from condenseit.config import HackerNewsConfig
from condenseit.pipeline.orchestrator import DigestPipeline
from condenseit.store.database import ContentStore
from condenseit.web.app import _attach_hn_links, _build_digest_detail


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


class HackerNewsLinksTest(unittest.TestCase):
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
                "content_hash": ContentStore.content_hash(
                    "Release details and discussion"
                ),
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
                    {
                        "digest_items": [
                            {
                                "url": original,
                                "title": "A useful release",
                                "source": "Hacker News (top)",
                                "summary": "Summary",
                            }
                        ]
                    }
                ),
            }
            item = _build_digest_detail(saved_digest, store)["items"][0]
            self.assertEqual(item["discussion_url"], discussion)
            self.assertEqual(item["original_url"], original)
            store.close()

    def test_collector_preserves_article_url_and_exposes_both_links(self):
        story = {
            "id": 42,
            "title": "A useful release",
            "score": 100,
            "url": "https://example.com/release",
            "time": 1_700_000_000,
        }
        collector = HackerNewsCollector([HackerNewsConfig(feed="top", min_score=1)])
        collector._client.close()
        collector._client = _Client({42: story})
        collector._extract_content = lambda url: "Release details."
        articles = collector._collect_feed(
            collector.sources[0],
            "top",
            "https://hacker-news.firebaseio.com/v0/topstories.json",
        )
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["url"], story["url"])
        self.assertEqual(article["original_url"], story["url"])
        self.assertEqual(
            article["discussion_url"], "https://news.ycombinator.com/item?id=42"
        )

    def test_self_post_has_discussion_link_without_original_article(self):
        story = {
            "id": 42,
            "title": "Ask HN: a question",
            "score": 100,
            "text": "Question body.",
        }
        collector = HackerNewsCollector([HackerNewsConfig(feed="top", min_score=1)])
        collector._client.close()
        collector._client = _Client({42: story})
        article = collector._collect_feed(
            collector.sources[0],
            "top",
            "https://hacker-news.firebaseio.com/v0/topstories.json",
        )[0]
        self.assertEqual(article["url"], article["discussion_url"])
        self.assertEqual(article["original_url"], "")

    def test_unchanged_article_refreshes_link_metadata_and_preserves_rating(self):
        with TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "test.db")
            article = {
                "url": "https://example.com/release",
                "title": "A useful release",
                "content": "Release details",
                "source": "Hacker News (top)",
                "category": "General",
                "content_hash": ContentStore.content_hash("Release details"),
                "published_at": "2026-09-28T10:00:00+00:00",
                "collected_at": "2026-09-28T10:01:00+00:00",
            }
            store.save_article(article)
            store.rate_article(article["url"], 5)
            store.mark_article_read(article["url"])
            incoming = {
                **article,
                "discussion_url": "https://news.ycombinator.com/item?id=42",
                "original_url": article["url"],
            }
            self.assertEqual(store.deduplicate([incoming]), [])
            saved = store.get_article(article["url"])
            self.assertEqual(saved["discussion_url"], incoming["discussion_url"])
            self.assertEqual(saved["original_url"], article["url"])
            self.assertEqual(saved["collected_at"], article["collected_at"])
            self.assertEqual(store.db["ratings"].get(article["url"])["rating"], 5)
            self.assertIn(article["url"], store.get_read_urls())
            store.close()

    def test_old_digest_falls_back_to_stored_url_when_metadata_is_empty(self):
        with TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "test.db")
            items = [
                {
                    "source": "Hacker News (top)",
                    "url": "https://news.ycombinator.com/item?id=42",
                    "discussion_url": "",
                },
                {
                    "source": "Hacker News (top)",
                    "url": "https://example.com/release",
                    "original_url": "",
                },
            ]
            linked = _attach_hn_links(store, items)
            self.assertEqual(linked[0]["discussion_url"], items[0]["url"])
            self.assertEqual(linked[1]["original_url"], items[1]["url"])
            store.close()

    def test_dry_run_digest_preserves_link_metadata(self):
        article = {
            "url": "https://example.com/release",
            "discussion_url": "https://news.ycombinator.com/item?id=42",
            "original_url": "https://example.com/release",
        }
        item = DigestPipeline._digest_items_dry_run([article], set(), [])[0]
        self.assertEqual(item["discussion_url"], article["discussion_url"])
        self.assertEqual(item["original_url"], article["original_url"])
