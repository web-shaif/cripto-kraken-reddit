import json
import unittest
from io import BytesIO

from reddit_collector import RedditCollector, RedditCredentials, RedditNotConfigured


class Response(BytesIO):
    headers = {}


class CollectorTests(unittest.TestCase):
    def test_missing_credentials(self):
        collector = RedditCollector(lambda: None, opener=lambda *_: self.fail("network access"))
        with self.assertRaises(RedditNotConfigured):
            collector.fetch(("Bitcoin",), 1000)

    def test_reads_public_listing(self):
        post = dict(id="ab1", subreddit="Bitcoin", title="Example title",
                    permalink="/r/Bitcoin/comments/ab1/example/", created_utc=990, is_self=True)

        def respond(request, timeout):
            if request.full_url == "https://www.reddit.com/api/v1/access_token":
                payload = {"access_token": "fixture", "expires_in": 3600}
            else:
                self.assertEqual(request.full_url, "https://oauth.reddit.com/r/Bitcoin/new?limit=100&raw_json=1")
                self.assertEqual(request.get_header("Authorization"), "Bearer fixture")
                payload = {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": post}]}}
            return Response(json.dumps(payload).encode())

        collector = RedditCollector(lambda: RedditCredentials("example", "dummy", "fixture_user"), opener=respond)
        result, = collector.fetch(("Bitcoin",), 1000)
        self.assertEqual((result.reddit_id, result.title, result.first_seen_at), ("ab1", "Example title", 1000))
