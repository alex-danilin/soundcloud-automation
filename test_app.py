import unittest
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

if __name__ == "__main__":
    unittest.main()
