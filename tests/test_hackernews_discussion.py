"""HN summaries include sampled comments without changing article URLs."""

import unittest

from condenseit.collectors.hackernews import HackerNewsCollector
from condenseit.config import HackerNewsConfig


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
    def test_article_and_comments_reach_summary_without_changing_url(self):
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
        collector._extract_content = lambda url: (
            "Release details: faster builds. " * 100
        )

        articles = collector._collect_feed(
            collector.sources[0],
            "top",
            "https://hacker-news.firebaseio.com/v0/topstories.json",
        )

        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["url"], "https://example.com/release")
        self.assertIn(
            "Original article: https://example.com/release", article["content"]
        )
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
