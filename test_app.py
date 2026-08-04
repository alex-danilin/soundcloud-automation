import unittest
import datetime
from unittest.mock import MagicMock, patch
from soundcloud_client import SoundCloudClient

class TestSoundCloudClient(unittest.TestCase):

    def test_key_extraction_camelot(self):
        track = {
            "title": "Deep Melodic Mix (128 BPM 8A)",
            "description": "Awesome techno track",
            "tag_list": "techno house 8A"
        }
        key = SoundCloudClient.extract_musical_key(track)
        self.assertEqual(key, "8A")

    def test_key_extraction_standard(self):
        track = {
            "title": "Summer Vibes",
            "description": "Key: F#m - Produced in 2026",
            "tag_list": "deep house"
        }
        key = SoundCloudClient.extract_musical_key(track)
        self.assertEqual(key, "F#m")

    def test_key_extraction_fallback(self):
        track = {
            "title": "No Key Track",
            "description": "Just a description with no musical key info",
            "tag_list": "ambient electronic"
        }
        key = SoundCloudClient.extract_musical_key(track)
        self.assertEqual(key, "Not specified")

    def test_key_signature_direct_property(self):
        track = {
            "key_signature": "C Major",
            "title": "Classical Piano",
            "description": "",
            "tag_list": ""
        }
        key = SoundCloudClient.extract_musical_key(track)
        self.assertEqual(key, "C Major")

    @patch("soundcloud_client.SoundCloudClient._make_request")
    def test_get_recent_likes_date_filtering(self, mock_make_request):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent_ts = (now - datetime.timedelta(minutes=10)).isoformat()
        old_ts = (now - datetime.timedelta(hours=5)).isoformat()
        naive_recent_ts = (now - datetime.timedelta(minutes=15)).strftime("%Y/%m/%d %H:%M:%S")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {"created_at": recent_ts, "track": {"id": 1, "title": "Recent Track ISO"}},
            {"created_at": old_ts, "track": {"id": 2, "title": "Old Track"}},
            {"created_at": naive_recent_ts, "track": {"id": 3, "title": "Recent Track Naive"}}
        ]
        mock_make_request.return_value = mock_resp

        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="at")
        tracks = client.get_recent_likes(lookback_minutes=60)

        track_ids = [t["id"] for t in tracks]
        self.assertIn(1, track_ids)
        self.assertIn(3, track_ids)
        self.assertNotIn(2, track_ids)

    @patch("soundcloud_client.SoundCloudClient._make_request")
    def test_playlist_caching(self, mock_make_request):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"id": 100, "title": "Genre: Techno"}]
        mock_make_request.return_value = mock_resp

        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="at")
        
        # First call fetches from API
        playlists1 = client.get_user_playlists()
        self.assertEqual(len(playlists1), 1)
        self.assertEqual(mock_make_request.call_count, 1)

        # Second call uses cache
        playlists2 = client.get_user_playlists()
        self.assertEqual(len(playlists2), 1)
        self.assertEqual(mock_make_request.call_count, 1)

        # Force refresh bypasses cache
        client.get_user_playlists(force_refresh=True)
        self.assertEqual(mock_make_request.call_count, 2)

    @patch("soundcloud_client.requests.post")
    @patch("soundcloud_client.requests.request")
    def test_401_token_retry(self, mock_requests, mock_post):
        # Mocks first request returning 401, second retry returning 200
        resp_401 = MagicMock()
        resp_401.status_code = 401
        
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.ok = True
        
        mock_requests.side_effect = [resp_401, resp_200]

        # Token refresh response
        post_resp = MagicMock()
        post_resp.ok = True
        post_resp.json.return_value = {"access_token": "new_access_token"}
        mock_post.return_value = post_resp

        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="old_at")
        res = client._make_request("GET", "https://api.soundcloud.com/me")
        
        self.assertEqual(res.status_code, 200)
        self.assertEqual(client.access_token, "new_access_token")
        self.assertEqual(mock_requests.call_count, 2)

if __name__ == "__main__":
    unittest.main()

