import re
import datetime
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from typing import List, Dict, Any, Optional, Tuple, Callable

logger = logging.getLogger("soundcloud_automation")

class SoundCloudClient:
    """Wrapper for SoundCloud API operations including Auth, Likes, Following, Playlists & Key Extraction."""

    BASE_URL = "https://api.soundcloud.com"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        access_token: str = "",
        playlist_prefix: str = "Genre: ",
        default_genre: str = "Uncategorized",
        playlist_sharing: str = "private",
        on_token_refresh: Optional[Callable[[str, str], None]] = None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.playlist_prefix = playlist_prefix
        self.default_genre = default_genre
        self.playlist_sharing = playlist_sharing if playlist_sharing in ("public", "private") else "private"
        self.on_token_refresh = on_token_refresh
        self._playlists_cache: Optional[List[Dict[str, Any]]] = None

        # Connection pooling and automatic retries for transient 429/5xx errors
        # H-3: Explicitly allow retries on POST for OAuth token refresh and notifications
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS", "TRACE"]),
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)

    def ensure_access_token(self, force_refresh: bool = False) -> str:
        """Obtains or refreshes the SoundCloud OAuth access token."""
        if self.access_token and not force_refresh:
            return self.access_token

        if not self.refresh_token or not self.client_id or not self.client_secret:
            raise ValueError("Missing SoundCloud API credentials (client_id, client_secret, or refresh_token).")

        token_url = f"{self.BASE_URL}/oauth2/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token
        }

        response = self.session.post(token_url, data=payload, timeout=15)
        if not response.ok:
            raise RuntimeError(f"Failed to refresh SoundCloud token: {response.status_code} - {response.text}")

        data = response.json()
        self.access_token = data.get("access_token", "")
        
        # Persist new refresh token if SoundCloud rotated it
        if "refresh_token" in data and data["refresh_token"] != self.refresh_token:
            self.refresh_token = data["refresh_token"]

        if self.on_token_refresh:
            try:
                self.on_token_refresh(self.access_token, self.refresh_token)
            except Exception as cb_err:
                logger.warning("Token refresh callback failed: %s", cb_err)

        return self.access_token

    def _get_headers(self) -> Dict[str, str]:
        token = self.ensure_access_token()
        return {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Executes HTTP request with connection reuse and token refresh on 401."""
        headers = kwargs.pop("headers", None)
        if headers is None:
            headers = self._get_headers()

        response = self.session.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and self.refresh_token:
            # Token expired; force refresh and retry once
            token = self.ensure_access_token(force_refresh=True)
            headers["Authorization"] = f"OAuth {token}"
            response = self.session.request(method, url, headers=headers, **kwargs)

        return response

    def get_recent_likes(
        self,
        lookback_minutes: int = 65,
        last_processed_like_id: Optional[int] = None,
        processed_ids: Optional[set] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches recently liked tracks.
        Likes are returned in reverse chronological order (most recently liked first).
        Paginates up to max_pages (default 10 pages / 500 tracks) following next_href.
        If last_processed_like_id or processed_ids is provided, stops scanning once seen likes are reached.
        """
        url: Optional[str] = f"{self.BASE_URL}/me/likes/tracks?limit=50&linked_partitioning=true"
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=lookback_minutes)
        recent_tracks = []
        stop_scanning = False
        page_count = 0
        max_pages = 10

        while url and not stop_scanning and page_count < max_pages:
            page_count += 1
            response = self._make_request("GET", url, timeout=15)
            if not response.ok:
                raise RuntimeError(f"Error fetching SoundCloud likes: {response.status_code} - {response.text}")

            data = response.json()
            if isinstance(data, dict):
                items = data.get("collection", [])
                url = data.get("next_href")
            elif isinstance(data, list):
                items = data
                url = None
            else:
                items = []
                url = None

            for item in items:
                if stop_scanning:
                    break

                # Handle both wrapped items {"created_at": ..., "track": {...}} and bare track objects
                is_wrapped = isinstance(item, dict) and "track" in item and isinstance(item["track"], dict)
                track = item["track"] if is_wrapped else item
                
                if not isinstance(track, dict) or "id" not in track:
                    continue

                track_id = track.get("id")

                # 1. State-based deduplication: stop if we hit the last processed track ID
                if last_processed_like_id and track_id == last_processed_like_id:
                    stop_scanning = True
                    break

                if processed_ids and track_id in processed_ids:
                    continue

                # 2. Recency time check: ONLY use item["created_at"] if item is a like-event wrapper!
                # If item is a bare track object, item["created_at"] is track UPLOAD time, not like time.
                if is_wrapped and "created_at" in item:
                    created_at_str = item["created_at"]
                    try:
                        dt_str = str(created_at_str).replace("/", "-").replace(" +0000", "+00:00")
                        if dt_str.endswith("Z"):
                            dt_str = dt_str[:-1] + "+00:00"
                        
                        liked_dt = datetime.datetime.fromisoformat(dt_str)
                        if liked_dt.tzinfo is None:
                            liked_dt = liked_dt.replace(tzinfo=datetime.timezone.utc)
                        
                        if liked_dt < cutoff_time:
                            stop_scanning = True
                            break
                    except Exception as parse_err:
                        logger.warning("Could not parse like timestamp '%s': %s", created_at_str, parse_err)
                        continue

                recent_tracks.append(track)

        if url and page_count >= max_pages and not stop_scanning:
            logger.warning("Reached max_pages limit (%d) when fetching SoundCloud likes. Some older likes were not scanned.", max_pages)

        return recent_tracks

    def follow_artist(self, artist_id: int) -> str:
        """
        Follows the specified artist by user ID on SoundCloud.
        Returns tri-state string: 'FOLLOWED', 'ALREADY_FOLLOWING', or 'FAILED'.
        """
        url = f"{self.BASE_URL}/me/followings/{artist_id}"
        
        response = self._make_request("PUT", url, timeout=15)
        if response.status_code in (200, 201, 204):
            return "FOLLOWED"
        elif response.status_code in (400, 422):
            # Already following or idempotent conflict
            return "ALREADY_FOLLOWING"
        else:
            logger.warning("Failed to follow artist %s: %s - %s", artist_id, response.status_code, response.text)
            return "FAILED"

    def get_user_playlists(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Retrieves the authenticated user's existing playlists with caching and pagination support."""
        if self._playlists_cache is not None and not force_refresh:
            return self._playlists_cache

        url: Optional[str] = f"{self.BASE_URL}/me/playlists?limit=50&linked_partitioning=true"
        all_playlists = []

        while url:
            response = self._make_request("GET", url, timeout=15)
            # H-8: Raise error on failed fetch instead of returning empty list
            if not response.ok:
                raise RuntimeError(f"Error fetching user playlists: {response.status_code} - {response.text}")
            
            data = response.json()
            if isinstance(data, list):
                all_playlists.extend(data)
                url = None
            elif isinstance(data, dict):
                collection = data.get("collection", [])
                all_playlists.extend(collection)
                # Paginate if next_href is present
                url = data.get("next_href")
            else:
                url = None

        self._playlists_cache = all_playlists
        return all_playlists

    def add_track_to_genre_playlist(self, track: Dict[str, Any], genre: str) -> Tuple[str, bool]:
        """
        Adds a track to a genre-specific playlist. Creates the playlist if it does not exist.
        Always fetches authoritative playlist tracks before updating to avoid data loss.
        Returns a tuple of (playlist_title, added_newly_boolean).
        """
        clean_genre = (genre or self.default_genre).strip()
        playlist_title = f"{self.playlist_prefix}{clean_genre}" if self.playlist_prefix else clean_genre
        track_id = track.get("id")

        if not track_id:
            raise ValueError("Track dictionary must contain a valid 'id'.")

        playlists = self.get_user_playlists()
        target_playlist = None

        for pl in playlists:
            if pl.get("title", "").strip().lower() == playlist_title.lower():
                target_playlist = pl
                break

        if target_playlist:
            playlist_id = target_playlist.get("id")
            if not playlist_id:
                raise RuntimeError(f"Playlist '{playlist_title}' exists in cache but has no valid ID.")

            # C-5: ALWAYS fetch full authoritative playlist details immediately before modifying!
            pl_resp = self._make_request("GET", f"{self.BASE_URL}/playlists/{playlist_id}", timeout=15)
            if not pl_resp.ok:
                raise RuntimeError(f"Cannot read playlist {playlist_id} before update: {pl_resp.status_code} - {pl_resp.text}")
            
            full_playlist_data = pl_resp.json()
            existing_tracks = full_playlist_data.get("tracks", [])

            # Handle both integer IDs [123, 456] and track dicts [{"id": 123}, ...]
            existing_ids = []
            for t in existing_tracks:
                if isinstance(t, int):
                    existing_ids.append(t)
                elif isinstance(t, dict) and "id" in t:
                    existing_ids.append(t["id"])

            if track_id in existing_ids:
                # Already in playlist
                return playlist_title, False

            # Add track to existing playlist
            updated_track_objs = [{"id": tid} for tid in existing_ids + [track_id]]
            payload = {"playlist": {"tracks": updated_track_objs}}
            
            update_url = f"{self.BASE_URL}/playlists/{playlist_id}"
            res = self._make_request("PUT", update_url, json=payload, timeout=15)
            if not res.ok:
                raise RuntimeError(f"Failed to update playlist '{playlist_title}': {res.status_code} - {res.text}")
            
            # V-4: Update cache in-place with authoritative updated data (removed dead variable assignment)
            target_playlist["tracks"] = existing_tracks + [{"id": track_id}]
            
            if self._playlists_cache is not None:
                for idx, pl in enumerate(self._playlists_cache):
                    if pl.get("id") == playlist_id:
                        self._playlists_cache[idx] = target_playlist
                        break

            return playlist_title, True
        else:
            # Create new playlist with track
            payload = {
                "playlist": {
                    "title": playlist_title,
                    "sharing": self.playlist_sharing,
                    "tracks": [{"id": track_id}]
                }
            }
            create_url = f"{self.BASE_URL}/playlists"
            res = self._make_request("POST", create_url, json=payload, timeout=15)
            if not res.ok:
                raise RuntimeError(f"Failed to create playlist '{playlist_title}': {res.status_code} - {res.text}")

            # H-1: Treat a response without a valid ID as a hard failure
            created_data = res.json() if res.text else {}
            created_id = created_data.get("id")
            if not created_id:
                raise RuntimeError(f"SoundCloud created playlist '{playlist_title}' but returned no valid playlist ID.")

            if self._playlists_cache is not None:
                self._playlists_cache.append(created_data)

            return playlist_title, True

    @staticmethod
    def extract_musical_key(track: Dict[str, Any]) -> str:
        """
        Extracts musical key (e.g. Camelot 8A/1B or pitch notation like C#m, Am, F Major)
        from track tag_list, title, description, or key attributes.
        """
        # 1. Direct API key property if present
        if track.get("key_signature"):
            return str(track.get("key_signature"))
        if track.get("key"):
            return str(track.get("key"))

        title = track.get("title", "")
        description = track.get("description", "")
        tag_list = track.get("tag_list", "")
        combined_text = f"{tag_list} {title} {description}"

        # 2. Camelot notation (e.g., 8A, 12B, [1B], (4A), Key: 8A)
        # Avoid false positives like "Vol 2B" or "Studio 8a" in general text by checking tags, brackets, or key prefixes.
        camelot_match = re.search(r'(?:key|camelot|scale)[:\s\-]+([1-9]|1[0-2])([AB])\b', combined_text, re.IGNORECASE)
        if camelot_match:
            return f"{camelot_match.group(1)}{camelot_match.group(2).upper()}"

        bracket_match = re.search(r'[\(\[]([1-9]|1[0-2])([AB])[\)\]]', combined_text, re.IGNORECASE)
        if bracket_match:
            return f"{bracket_match.group(1)}{bracket_match.group(2).upper()}"

        # Check tag_list specifically for bare Camelot key (e.g. "techno house 8A")
        tag_match = re.search(r'\b([1-9]|1[0-2])([AB])\b', tag_list, re.IGNORECASE)
        if tag_match:
            return f"{tag_match.group(1)}{tag_match.group(2).upper()}"

        # 3. Standard pitch notation (e.g. Key: C Major, Key - F#m, [Am], (D minor))
        pitch_key_match = re.search(
            r'\bkey[:\s\-]+([A-G][#b]?(?:\s*(?:m|min|maj|minor|major))?)\b',
            combined_text,
            re.IGNORECASE
        )
        if pitch_key_match:
            raw_key = pitch_key_match.group(1).strip()
            parts = raw_key.split()
            if len(parts) == 2:
                return f"{parts[0].capitalize()} {parts[1].capitalize()}"
            return raw_key.capitalize()

        pitch_bracket_match = re.search(
            r'[\(\[]([A-G][#b]?(?:\s*(?:m|min|maj|minor|major)?))[\)\]]',
            combined_text,
            re.IGNORECASE
        )
        if pitch_bracket_match:
            raw_key = pitch_bracket_match.group(1).strip()
            parts = raw_key.split()
            if len(parts) == 2:
                return f"{parts[0].capitalize()} {parts[1].capitalize()}"
            return raw_key.capitalize()

        return "Not specified"
