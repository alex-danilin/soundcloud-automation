import unittest
import datetime
from unittest.mock import MagicMock, patch
from soundcloud_client import SoundCloudClient
from telegram_notifier import TelegramNotifier
import state_manager

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

    def test_key_extraction_full_major(self):
        track = {
            "title": "Piano Sonata",
            "description": "Key: C Major",
            "tag_list": "classical"
        }
        key = SoundCloudClient.extract_musical_key(track)
        self.assertEqual(key, "C Major")

    def test_key_extraction_fallback(self):
        track = {
            "title": "Vol 2B Continued",
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

    @patch.object(SoundCloudClient, "_make_request")
    def test_get_recent_likes_with_state_boundary(self, mock_make_request):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "collection": [
                {"id": 100, "title": "Track 100"},
                {"id": 99, "title": "Track 99"},
                {"id": 98, "title": "Track 98 (Last Processed)"},
                {"id": 97, "title": "Track 97 (Old)"}
            ]
        }
        mock_make_request.return_value = mock_resp

        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="at")
        tracks = client.get_recent_likes(last_processed_like_id=98)

        track_ids = [t["id"] for t in tracks]
        self.assertEqual(track_ids, [100, 99])

    @patch.object(SoundCloudClient, "_make_request")
    def test_playlist_authoritative_fetch_and_update(self, mock_make_request):
        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="at")
        client._playlists_cache = [
            {"id": 500, "title": "Genre: Techno", "tracks": []}
        ]

        get_pl_resp = MagicMock()
        get_pl_resp.ok = True
        get_pl_resp.json.return_value = {"id": 500, "title": "Genre: Techno", "tracks": [101, 102]}

        put_pl_resp = MagicMock()
        put_pl_resp.ok = True

        mock_make_request.side_effect = [get_pl_resp, put_pl_resp]

        title, added = client.add_track_to_genre_playlist({"id": 103}, "Techno")
        self.assertEqual(title, "Genre: Techno")
        self.assertTrue(added)

        self.assertEqual(mock_make_request.call_count, 2)
        put_call_args = mock_make_request.call_args_list[1]
        payload = put_call_args[1]["json"]
        self.assertEqual(payload["playlist"]["tracks"], [{"id": 101}, {"id": 102}, {"id": 103}])

    def test_telegram_tri_state_and_url_escaping(self):
        notifier = TelegramNotifier(bot_token="test_token", chat_id="12345")
        with patch.object(notifier.session, "post") as mock_post:
            mock_post.return_value.ok = True
            track = {
                "title": "Track & Roll",
                "permalink_url": "https://soundcloud.com/artist/track?param=1&other=2",
                "user": {
                    "username": "Artist <One>",
                    "permalink_url": "https://soundcloud.com/artist?ref=1&type=user"
                }
            }
            res = notifier.send_track_notification(track, "Genre: Rock & Roll", "ALREADY_FOLLOWING", "8A")
            self.assertTrue(res)
            
            sent_payload = mock_post.call_args[1]["json"]
            sent_text = sent_payload["text"]
            self.assertIn("https://soundcloud.com/artist/track?param=1&amp;other=2", sent_text)
            self.assertIn("Artist &lt;One&gt;", sent_text)
            self.assertIn("ℹ️ Already Following", sent_text)

    def test_state_manager_fifo_capping(self):
        state = {
            "last_processed_like_id": 1000,
            "notified_track_ids": list(range(1, 601))  # 600 items in order 1..600
        }
        mock_storage = MagicMock()
        mock_blob = MagicMock()
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        with patch.object(state_manager, "storage", mock_storage):
            state_manager.save_state("test-bucket", state)
            self.assertTrue(mock_blob.upload_from_string.called)
            
            uploaded_json = mock_blob.upload_from_string.call_args[0][0]
            import json
            saved_data = json.loads(uploaded_json)
            # Must keep the NEWEST 500 (101 to 600) in insertion order
            self.assertEqual(len(saved_data["notified_track_ids"]), 500)
            self.assertEqual(saved_data["notified_track_ids"][0], 101)
            self.assertEqual(saved_data["notified_track_ids"][-1], 600)

if __name__ == "__main__":
    unittest.main()
