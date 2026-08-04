import os
import logging
from dotenv import load_dotenv

# Load local .env file if available (useful for local development)
load_dotenv()

def _parse_int(val: str, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        logging.warning("Invalid integer env var value '%s', defaulting to %d", val, default)
        return default

class Config:
    """Application Configuration loaded from Environment / Secrets Manager."""
    
    # SoundCloud API Credentials
    SOUNDCLOUD_CLIENT_ID = os.getenv("SOUNDCLOUD_CLIENT_ID", "")
    SOUNDCLOUD_CLIENT_SECRET = os.getenv("SOUNDCLOUD_CLIENT_SECRET", "")
    SOUNDCLOUD_REFRESH_TOKEN = os.getenv("SOUNDCLOUD_REFRESH_TOKEN", "")
    SOUNDCLOUD_ACCESS_TOKEN = os.getenv("SOUNDCLOUD_ACCESS_TOKEN", "")
    
    # Telegram Bot Configuration
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # Application Behavior Settings
    LOOKBACK_MINUTES = _parse_int(os.getenv("LOOKBACK_MINUTES", "65"), 65)
    
    # Playlist Naming Prefix (e.g. "Genre: Techno" or "" for just "Techno")
    PLAYLIST_PREFIX = os.getenv("PLAYLIST_PREFIX", "Genre: ")
    
    # Fallback Genre when track has no genre assigned
    DEFAULT_GENRE = os.getenv("DEFAULT_GENRE", "Uncategorized")

    # Playlist Privacy / Sharing setting ("private" or "public", default: "private")
    PLAYLIST_SHARING = os.getenv("PLAYLIST_SHARING", "private")

    # GCP State Storage Bucket for tracking last processed likes and deduplication
    STATE_BUCKET = os.getenv("STATE_BUCKET", "")
    PROJECT_ID = os.getenv("PROJECT_ID", "") or os.getenv("GOOGLE_CLOUD_PROJECT", "") or os.getenv("GCP_PROJECT", "")
