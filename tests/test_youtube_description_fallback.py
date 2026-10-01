"""Reject link-only YouTube fallbacks while allowing later retries."""

from unittest.mock import Mock

from condenseit.collectors.youtube import YouTubeCollector
from condenseit.config import YouTubeChannelConfig


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
