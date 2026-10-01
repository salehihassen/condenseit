"""Tests for YouTube RSS description extraction."""

from __future__ import annotations

from unittest.mock import Mock, patch

from condenseit.collectors.youtube import YouTubeCollector
from condenseit.config import YouTubeChannelConfig


def test_entry_plain_text_from_summary_detail_strips_html() -> None:
    entry = {
        "summary_detail": {
            "type": "text/html",
            "value": "<p>Hello &amp; <b>world</b></p>",
        },
    }
    assert YouTubeCollector._entry_plain_text(entry) == "Hello & world"


def test_entry_plain_text_falls_back_to_summary() -> None:
    entry = {"summary": "  Plain summary text  "}
    assert YouTubeCollector._entry_plain_text(entry) == "Plain summary text"


def test_entry_plain_text_falls_back_to_content_list() -> None:
    entry = {
        "content": [{"type": "text/html", "value": "<div>From content</div>"}],
    }
    assert YouTubeCollector._entry_plain_text(entry) == "From content"


def test_entry_plain_text_empty() -> None:
    assert YouTubeCollector._entry_plain_text({}) == ""


def test_fetch_transcript_uses_current_api() -> None:
    class Snippet:
        text = "A real transcript sentence."

    with patch("condenseit.collectors.youtube.YouTubeTranscriptApi") as api:
        api.return_value.fetch.return_value = [Snippet()]
        assert YouTubeCollector._fetch_transcript("MdkyCt6SygQ") == Snippet.text
        api.return_value.fetch.assert_called_once_with("MdkyCt6SygQ")


def test_link_only_description_is_not_summarizable() -> None:
    description = (
        "Spotify - https://open.spotify.com/show/example "
        "Apple Podcasts - https://podcasts.apple.com/example "
        "LinkedIn at - https://www.linkedin.com/in/example"
    )
    assert not YouTubeCollector._description_has_substance(description)
    assert YouTubeCollector._description_has_substance(
        "In this video I explain the data center payment dispute and why "
        "the original contract terms matter for Oracle's construction plans."
    )


def test_link_only_video_is_skipped_and_remains_retryable() -> None:
    feed = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Oracle data center project</title>
        <link href="https://www.youtube.com/watch?v=MdkyCt6SygQ" />
        <content>Spotify - https://open.spotify.com/show/example
        Apple Podcasts - https://podcasts.apple.com/example</content>
      </entry>
    </feed>"""
    collector = YouTubeCollector.__new__(YouTubeCollector)
    collector._transcription = None
    collector._or_key = ""
    collector._get_feed_with_retry = Mock(return_value=Mock(text=feed))
    collector._is_processed = Mock(return_value=False)
    collector._fetch_transcript = Mock(return_value="")
    collector._mark_processed = Mock()

    videos = collector._collect_channel(YouTubeChannelConfig(channel_id="test"))

    assert videos == []
    collector._mark_processed.assert_not_called()
