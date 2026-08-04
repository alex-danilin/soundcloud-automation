import html
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from typing import Dict, Any, Optional, Union

logger = logging.getLogger("soundcloud_automation")

def _safe_url(url: Any) -> str:
    url_str = str(url or "").strip()
    if url_str.startswith("http://") or url_str.startswith("https://"):
        return html.escape(url_str, quote=True)
    return "#"

class TelegramNotifier:
    """Sends HTML formatted notifications to a Telegram Chat via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        
        # Connection pooling and automatic retries for Telegram API
        # H-3: Retries apply to POST requests for sendMessage
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS", "TRACE"]),
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)

    def send_track_notification(
        self,
        track: Dict[str, Any],
        playlist_title: str,
        artist_follow_status: Union[str, bool],
        musical_key: str
    ) -> str:
        """
        Formats and posts a notification message for a processed SoundCloud track.
        Returns tri-state string: 'SENT', 'SKIPPED', or 'FAILED'.
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram notification skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
            return "SKIPPED"

        title = html.escape(track.get("title", "Unknown Track"))
        permalink_url = _safe_url(track.get("permalink_url"))
        
        user_info = track.get("user", {})
        artist_name = html.escape(user_info.get("username", "Unknown Artist"))
        artist_url = _safe_url(user_info.get("permalink_url") or track.get("permalink_url"))

        genre = html.escape(track.get("genre", "Uncategorized") or "Uncategorized")
        playlist_escaped = html.escape(playlist_title)
        key_escaped = html.escape(musical_key)

        # M-8: Render tri-state follow status distinctly
        if artist_follow_status is True or artist_follow_status == "FOLLOWED":
            status_str = "✅ Followed"
        elif artist_follow_status == "ALREADY_FOLLOWING":
            status_str = "ℹ️ Already Following"
        elif artist_follow_status == "FAILED":
            status_str = "⚠️ Follow Attempt Failed"
        else:
            status_str = "⚠️ Skipped"

        message = (
            f"🎵 <b>SoundCloud Track Liked & Processed!</b>\n\n"
            f"🎧 <b>Track:</b> <a href=\"{permalink_url}\">{title}</a>\n"
            f"👤 <b>Artist:</b> <a href=\"{artist_url}\">{artist_name}</a>\n"
            f"📁 <b>Genre Playlist:</b> {playlist_escaped}\n"
            f"🎼 <b>Musical Key:</b> {key_escaped}\n"
            f"➕ <b>Artist Status:</b> {status_str}\n\n"
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
            response = self.session.post(api_url, json=payload, timeout=10)
            if not response.ok:
                logger.error("Telegram API Error: %s - %s", response.status_code, response.text)
                return "FAILED"
            return "SENT"
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e)
            return "FAILED"
