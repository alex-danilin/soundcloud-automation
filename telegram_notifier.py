import html
import requests
from typing import Dict, Any, Optional

class TelegramNotifier:
    """Sends HTML formatted notifications to a Telegram Chat via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send_track_notification(
        self,
        track: Dict[str, Any],
        playlist_title: str,
        artist_followed: bool,
        musical_key: str
    ) -> bool:
        """
        Formats and posts a notification message for a processed SoundCloud track.
        """
        if not self.bot_token or not self.chat_id:
            print("Telegram notification skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
            return False

        title = html.escape(track.get("title", "Unknown Track"))
        permalink_url = track.get("permalink_url", "#")
        
        user_info = track.get("user", {})
        artist_name = html.escape(user_info.get("username", "Unknown Artist"))
        artist_url = user_info.get("permalink_url", permalink_url)

        genre = html.escape(track.get("genre", "Uncategorized") or "Uncategorized")
        playlist_escaped = html.escape(playlist_title)
        key_escaped = html.escape(musical_key)

        follow_status = "✅ Followed" if artist_followed else "⚠️ Already Following / Skipped"

        message = (
            f"🎵 <b>SoundCloud Track Liked & Processed!</b>\n\n"
            f"🎧 <b>Track:</b> <a href=\"{permalink_url}\">{title}</a>\n"
            f"👤 <b>Artist:</b> <a href=\"{artist_url}\">{artist_name}</a>\n"
            f"📁 <b>Genre Playlist:</b> {playlist_escaped}\n"
            f"🎼 <b>Musical Key:</b> {key_escaped}\n"
            f"➕ <b>Artist Status:</b> {follow_status}\n\n"
            f"🔗 <a href=\"{permalink_url}\">Open in SoundCloud</a>"
        )

        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        try:
            response = requests.post(api_url, json=payload, timeout=10)
            if not response.ok:
                print(f"Telegram API Error: {response.status_code} - {response.text}")
                return False
            return True
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
            return False
