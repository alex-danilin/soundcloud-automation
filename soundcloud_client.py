import re
import datetime
import requests
from typing import List, Dict, Any, Optional, Tuple

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
        default_genre: str = "Uncategorized"
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.playlist_prefix = playlist_prefix
        self.default_genre = default_genre
        self._playlists_cache: Optional[List[Dict[str, Any]]] = None

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

        response = requests.post(token_url, data=payload, timeout=15)
        if not response.ok:
            raise RuntimeError(f"Failed to refresh SoundCloud token: {response.status_code} - {response.text}")

        data = response.json()
        self.access_token = data.get("access_token", "")
        # Optionally update refresh token if a new one was issued
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]

        return self.access_token

    def _get_headers(self) -> Dict[str, str]:
        token = self.ensure_access_token()
        return {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Executes HTTP request with automatic single retry on 401 Unauthorized via token refresh."""
        headers = kwargs.pop("headers", None)
        if headers is None:
            headers = self._get_headers()

        response = requests.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and self.refresh_token:
            # Token expired; force refresh and retry once
            token = self.ensure_access_token(force_refresh=True)
            headers["Authorization"] = f"OAuth {token}"
            response = requests.request(method, url, headers=headers, **kwargs)

        return response

    def get_recent_likes(self, lookback_minutes: int = 65) -> List[Dict[str, Any]]:
        """
        Fetches recently liked tracks within the specified lookback window.
        Filters tracks based on the 'created_at' timestamp of the like event.
        """
        url = f"{self.BASE_URL}/me/likes/tracks?limit=50"
        
        response = self._make_request("GET", url, timeout=15)
        if not response.ok:
            # Fallback to alternate endpoint if /me/likes/tracks returns error
            url = f"{self.BASE_URL}/me/favorites?limit=50"
            response = self._make_request("GET", url, timeout=15)
            if not response.ok:
                raise RuntimeError(f"Error fetching SoundCloud likes: {response.status_code} - {response.text}")

        data = response.json()
        items = data.get("collection", data) if isinstance(data, dict) else data
        
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=lookback_minutes)
        recent_tracks = []

        for item in items:
            track = item.get("track", item) if isinstance(item, dict) and "track" in item else item
            if not isinstance(track, dict) or "id" not in track:
                continue

            # Extract timestamp of when the track was liked or created
            created_at_str = item.get("created_at") or track.get("created_at")
            if created_at_str:
                try:
                    dt_str = str(created_at_str).replace("/", "-").replace(" +0000", "+00:00")
                    if dt_str.endswith("Z"):
                        dt_str = dt_str[:-1] + "+00:00"
                    
                    liked_dt = datetime.datetime.fromisoformat(dt_str)
                    if liked_dt.tzinfo is None:
                        liked_dt = liked_dt.replace(tzinfo=datetime.timezone.utc)
                    
                    if liked_dt < cutoff_time:
                        continue
                except Exception as parse_err:
                    print(f"Warning: Could not parse timestamp '{created_at_str}': {parse_err}")

            recent_tracks.append(track)

        return recent_tracks

    def follow_artist(self, artist_id: int) -> bool:
        """
        Follows the specified artist by user ID on SoundCloud.
        Idempotent: Following an already followed artist succeeds gracefully.
        """
        url = f"{self.BASE_URL}/me/followings/{artist_id}"
        
        response = self._make_request("PUT", url, timeout=15)
        if response.status_code in (200, 201, 204):
            return True
        
        # Fallback endpoint
        alt_url = f"{self.BASE_URL}/users/{artist_id}/follow"
        alt_resp = self._make_request("POST", alt_url, timeout=15)
        return alt_resp.status_code in (200, 201, 204)

    def get_user_playlists(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Retrieves the authenticated user's existing playlists with caching."""
        if self._playlists_cache is not None and not force_refresh:
            return self._playlists_cache

        url = f"{self.BASE_URL}/me/playlists"
        
        response = self._make_request("GET", url, timeout=15)
        if not response.ok:
            return []
        
        data = response.json()
        playlists = data if isinstance(data, list) else data.get("collection", [])
        self._playlists_cache = playlists
        return playlists

    def add_track_to_genre_playlist(self, track: Dict[str, Any], genre: str) -> Tuple[str, bool]:
        """
        Adds a track to a genre-specific playlist. Creates the playlist if it does not exist.
        Returns a tuple of (playlist_title, added_newly_boolean).
        """
        clean_genre = (genre or self.default_genre).strip()
        playlist_title = f"{self.playlist_prefix}{clean_genre}" if self.playlist_prefix else clean_genre
        track_id = track.get("id")

        playlists = self.get_user_playlists()
        target_playlist = None

        for pl in playlists:
            if pl.get("title", "").strip().lower() == playlist_title.lower():
                target_playlist = pl
                break

        if target_playlist:
            playlist_id = target_playlist.get("id")
            existing_tracks = target_playlist.get("tracks")
            
            # If summary object did not include full tracks array, fetch playlist details
            if existing_tracks is None and playlist_id:
                pl_resp = self._make_request("GET", f"{self.BASE_URL}/playlists/{playlist_id}", timeout=15)
                if pl_resp.ok:
                    target_playlist = pl_resp.json()
                    existing_tracks = target_playlist.get("tracks", [])
                else:
                    existing_tracks = []
            elif existing_tracks is None:
                existing_tracks = []

            existing_ids = [t.get("id") for t in existing_tracks if isinstance(t, dict) and "id" in t]

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
            
            self._playlists_cache = None
            return playlist_title, True
        else:
            # Create new playlist with track
            payload = {
                "playlist": {
                    "title": playlist_title,
                    "sharing": "public",
                    "tracks": [{"id": track_id}]
                }
            }
            create_url = f"{self.BASE_URL}/playlists"
            res = self._make_request("POST", create_url, json=payload, timeout=15)
            if not res.ok:
                # Try fallback create endpoint
                create_url = f"{self.BASE_URL}/me/playlists"
                res = self._make_request("POST", create_url, json=payload, timeout=15)
                if not res.ok:
                    raise RuntimeError(f"Failed to create playlist '{playlist_title}': {res.status_code} - {res.text}")

            self._playlists_cache = None
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

        combined_text = f"{track.get('tag_list', '')} {track.get('title', '')} {track.get('description', '')}"

        # 2. Check for Camelot key notations (e.g. 1A, 8B, 12A)
        camelot_match = re.search(r'\b(1[0-2]|[1-9])([AB])\b', combined_text, re.IGNORECASE)
        if camelot_match:
            return camelot_match.group(0).upper()

        # 3. Check for standard musical keys (e.g. Key: Am or Key - C#m)
        key_label_match = re.search(r'\bkey[:\s\-]+([A-G][#b]?(?:m|min|maj|minor|major)?)\b', combined_text, re.IGNORECASE)
        if key_label_match:
            return key_label_match.group(1).capitalize()

        return "Not specified"

