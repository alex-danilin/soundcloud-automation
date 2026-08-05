import flask
import unittest
import datetime
from unittest.mock import MagicMock, patch
from soundcloud_client import SoundCloudClient
from telegram_notifier import TelegramNotifier
import state_manager

_TEST_APP = flask.Flask(__name__)

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

    def test_proactive_token_expiry_refresh(self):
        client = SoundCloudClient(client_id="cid", client_secret="cs", refresh_token="rt", access_token="")
        client._token_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)

        with patch.object(client.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"access_token": "new_at", "expires_in": 3600}
            mock_post.return_value = mock_resp

            token = client.ensure_access_token()
            self.assertEqual(token, "new_at")
            self.assertTrue(mock_post.called)
            self.assertIsNotNone(client._token_expires_at)
            self.assertTrue(client._token_is_fresh())

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
            self.assertEqual(res, "SENT")
            
            sent_payload = mock_post.call_args[1]["json"]
            sent_text = sent_payload["text"]
            self.assertIn("https://soundcloud.com/artist/track?param=1&amp;other=2", sent_text)
            self.assertIn("Artist &lt;One&gt;", sent_text)
            self.assertIn("ℹ️ Already Following", sent_text)

    def test_telegram_unconfigured_returns_skipped(self):
        notifier = TelegramNotifier(bot_token="", chat_id="")
        res = notifier.send_track_notification({}, "Genre: Techno", "FOLLOWED", "8A")
        self.assertEqual(res, "SKIPPED")

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

    def test_state_manager_not_found_returns_empty(self):
        from google.api_core import exceptions as gcp_exceptions
        mock_storage = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_as_bytes.side_effect = gcp_exceptions.NotFound("Object not found")
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        with patch.object(state_manager, "storage", mock_storage):
            res = state_manager.load_state("test-bucket")
            self.assertEqual(res, {})

    def test_state_manager_corrupt_json_raises_runtime_error(self):
        mock_storage = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_as_bytes.return_value = b"NOT_VALID_JSON"
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        with patch.object(state_manager, "storage", mock_storage):
            with self.assertRaises(RuntimeError):
                state_manager.load_state("test-bucket")

    @patch("main.Config")
    @patch("main.SoundCloudClient")
    @patch("main.TelegramNotifier")
    @patch("main.state_manager")
    def test_main_state_transitions_table_driven(self, mock_state_mgr, mock_tg, mock_sc_class, mock_config):
        test_cases = [
            {
                "name": "Clean run: marker advances to 110",
                "playlist_fail": False,
                "tg_status": "SENT",
                "follow_status": "FOLLOWED",
                "expected_marker": 110
            },
            {
                "name": "Playlist failure: marker held at 100",
                "playlist_fail": True,
                "tg_status": "SENT",
                "follow_status": "FOLLOWED",
                "expected_marker": 100
            },
            {
                "name": "Telegram send API failure: marker held at 100",
                "playlist_fail": False,
                "tg_status": "FAILED",
                "follow_status": "FOLLOWED",
                "expected_marker": 100
            },
            {
                "name": "Telegram unconfigured (SKIPPED): marker advances cleanly to 110",
                "playlist_fail": False,
                "tg_status": "SKIPPED",
                "follow_status": "FOLLOWED",
                "expected_marker": 110
            },
            {
                "name": "Artist follow failure: marker held at 100",
                "playlist_fail": False,
                "tg_status": "SENT",
                "follow_status": "FAILED",
                "expected_marker": 100
            }
        ]

        for tc in test_cases:
            with self.subTest(case=tc["name"]):
                mock_config.SOUNDCLOUD_CLIENT_ID = "cid"
                mock_config.SOUNDCLOUD_CLIENT_SECRET = "cs"
                mock_config.SOUNDCLOUD_REFRESH_TOKEN = "rt"
                mock_config.TELEGRAM_BOT_TOKEN = "tb"
                mock_config.TELEGRAM_CHAT_ID = "tc"
                mock_config.LOOKBACK_MINUTES = 65
                mock_config.PLAYLIST_PREFIX = "Genre: "
                mock_config.DEFAULT_GENRE = "Uncategorized"
                mock_config.PLAYLIST_SHARING = "private"
                mock_config.STATE_BUCKET = "my-test-bucket"

                mock_state_mgr.load_state.return_value = {
                    "last_processed_like_id": 100,
                    "notified_track_ids": [100]
                }

                mock_sc_instance = MagicMock()
                mock_sc_class.return_value = mock_sc_instance
                mock_sc_instance.get_recent_likes.return_value = [
                    {"id": 110, "title": "Track 110", "genre": "Techno", "user": {"id": 888}},
                    {"id": 109, "title": "Track 109", "genre": "Techno", "user": {"id": 999}}
                ]
                mock_sc_instance.follow_artist.return_value = tc["follow_status"]
                mock_sc_instance.extract_musical_key.return_value = "8A"

                if tc["playlist_fail"]:
                    def fail_add(track, genre):
                        if track["id"] == 109:
                            raise RuntimeError("Playlist API Error")
                        return "Genre: Techno", True
                    mock_sc_instance.add_track_to_genre_playlist.side_effect = fail_add
                else:
                    mock_sc_instance.add_track_to_genre_playlist.side_effect = None
                    mock_sc_instance.add_track_to_genre_playlist.return_value = ("Genre: Techno", True)

                mock_tg_instance = MagicMock()
                mock_tg.return_value = mock_tg_instance
                mock_tg_instance.send_track_notification.return_value = tc["tg_status"]

                from main import main
                mock_req = MagicMock()
                mock_req.args = {}
                mock_req.get_json.return_value = {}

                with _TEST_APP.app_context():
                    resp_data, status_code = main(mock_req)

                saved_state = mock_state_mgr.save_state.call_args[0][1]
                self.assertEqual(saved_state["last_processed_like_id"], tc["expected_marker"])

if __name__ == "__main__":
    unittest.main()
