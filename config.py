import os
from dotenv import load_dotenv

# Load local .env file if available (useful for local development)
load_dotenv()

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
    # Lookback window in minutes (default: 65 mins for hourly cron runs)
    LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "65"))
    
    # Playlist Naming Prefix (e.g. "Genre: Techno" or "" for just "Techno")
    PLAYLIST_PREFIX = os.getenv("PLAYLIST_PREFIX", "Genre: ")
    
    # Fallback Genre when track has no genre assigned
    DEFAULT_GENRE = os.getenv("DEFAULT_GENRE", "Uncategorized")
