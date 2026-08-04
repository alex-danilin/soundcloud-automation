import os
import json
import datetime
import logging
import functions_framework
from flask import jsonify, Flask, request as flask_request
from config import Config
from soundcloud_client import SoundCloudClient
from telegram_notifier import TelegramNotifier
import state_manager

# M-14: Custom Cloud Logging Formatter emitting JSON with severity for Cloud Run
class CloudLoggingFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "name": record.name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        if record.exc_info:
            log_entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

# Configure logging for Cloud Run / Functions Framework
handler = logging.StreamHandler()
if os.environ.get("K_SERVICE") or os.environ.get("PORT"):
    handler.setFormatter(CloudLoggingFormatter())
else:
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logger = logging.getLogger("soundcloud_automation")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(handler)

def _get_gcp_project_id() -> str:
    """Resolves GCP Project ID from env vars or Cloud Run metadata server."""
    if Config.PROJECT_ID:
        return Config.PROJECT_ID

    for env_key in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "PROJECT_ID"):
        if os.environ.get(env_key):
            return os.environ[env_key]

    try:
        import requests
        resp = requests.get(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"},
            timeout=2
        )
        if resp.ok and resp.text:
            return resp.text.strip()
    except Exception:
        pass

    return ""

def _persist_refreshed_token(new_access_token: str, new_refresh_token: str):
    """
    Callback triggered when SoundCloud issues a new access or rotated refresh token.
    Persists the new refresh token to GCP Secret Manager.
    """
    logger.info("SoundCloud token refreshed successfully. Updating rotated refresh token in Secret Manager.")
    project_id = _get_gcp_project_id()
    secret_id = "soundcloud-refresh-token"
    
    if not project_id:
        logger.warning("GCP Project ID could not be determined. Skipping Secret Manager token persistence.")
        return

    if not new_refresh_token:
        return

    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{project_id}/secrets/{secret_id}"
        response = client.add_secret_version(
            request={"parent": parent, "payload": {"data": new_refresh_token.encode("UTF-8")}}
        )
        logger.info("Successfully persisted rotated refresh token version to Secret Manager: %s", response.name)
    except Exception as sm_err:
        logger.error("FAILED to persist rotated refresh token to Secret Manager (%s). "
                     "Ensure runtime SA has roles/secretmanager.secretVersionAdder.", sm_err)

@functions_framework.http
def main(request_obj):
    """
    HTTP Cloud Function / Cloud Run entry point.
    Triggered periodically via GCP Cloud Scheduler or HTTP GET/POST.
    """
    # 1. Parse & validate optional lookback minutes parameter
    lookback = Config.LOOKBACK_MINUTES
    
    if request_obj:
        args = request_obj.args or {}
        json_data = request_obj.get_json(silent=True) or {}
        custom_lookback = args.get("lookback_minutes") or json_data.get("lookback_minutes")
        if custom_lookback:
            try:
                parsed_val = int(custom_lookback)
                lookback = max(1, min(parsed_val, 10080))
            except (ValueError, TypeError):
                logger.warning("Invalid lookback_minutes parameter provided: %s", custom_lookback)

    # V-3: Warn prominently if STATE_BUCKET is empty
    if not Config.STATE_BUCKET:
        logger.warning("STATE_BUCKET environment variable is empty! Running in fallback mode without GCS persistent state. "
                       "Tracks will be deduplicated by playlist membership only.")

    # 2. Check essential credentials
    if not Config.SOUNDCLOUD_CLIENT_ID or not Config.SOUNDCLOUD_CLIENT_SECRET or not Config.SOUNDCLOUD_REFRESH_TOKEN:
        logger.error("SoundCloud credentials missing.")
        return jsonify({
            "status": "error",
            "message": "SoundCloud API credentials (client_id, client_secret, refresh_token) are missing."
        }), 500

    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing. Notifications will be skipped.")

    sc_client = SoundCloudClient(
        client_id=Config.SOUNDCLOUD_CLIENT_ID,
        client_secret=Config.SOUNDCLOUD_CLIENT_SECRET,
        refresh_token=Config.SOUNDCLOUD_REFRESH_TOKEN,
        access_token=Config.SOUNDCLOUD_ACCESS_TOKEN,
        playlist_prefix=Config.PLAYLIST_PREFIX,
        default_genre=Config.DEFAULT_GENRE,
        playlist_sharing=Config.PLAYLIST_SHARING,
        on_token_refresh=_persist_refreshed_token
    )

    telegram = TelegramNotifier(
        bot_token=Config.TELEGRAM_BOT_TOKEN,
        chat_id=Config.TELEGRAM_CHAT_ID
    )

    # 3. Load state from GCS bucket if configured
    app_state = state_manager.load_state(Config.STATE_BUCKET)
    last_processed_like_id = app_state.get("last_processed_like_id")

    # V-2: Preserve insertion order in list for FIFO capping, use set for O(1) membership lookup
    notified_track_ids = list(app_state.get("notified_track_ids", []))
    notified_lookup = set(notified_track_ids)

    processed_summary = []

    try:
        # 4. Fetch recent likes (passing state for exact position boundary)
        recent_tracks = sc_client.get_recent_likes(
            lookback_minutes=lookback,
            last_processed_like_id=last_processed_like_id,
            processed_ids=notified_lookup
        )
        logger.info("Found %d liked tracks to process.", len(recent_tracks))

        run_had_failures = False
        new_highest_like_id = recent_tracks[0]["id"] if recent_tracks and "id" in recent_tracks[0] else last_processed_like_id

        for track in recent_tracks:
            track_id = track.get("id")
            if not track_id:
                continue

            track_title = track.get("title", "Unknown Track")
            artist_id = track.get("user", {}).get("id")
            genre = track.get("genre") or Config.DEFAULT_GENRE

            # Action A: Follow the artist (M-8 tri-state)
            follow_status = "SKIPPED"
            if artist_id:
                try:
                    follow_status = sc_client.follow_artist(artist_id)
                    # V-7: If artist follow fails, mark failure so follow attempt can be retried
                    if follow_status == "FAILED":
                        run_had_failures = True
                except Exception as e:
                    logger.error("Error following artist %s: %s", artist_id, e)
                    follow_status = "FAILED"
                    run_had_failures = True

            # Action B: Add track to genre playlist (or create playlist)
            playlist_title = f"{Config.PLAYLIST_PREFIX}{genre}"
            playlist_added = False
            try:
                playlist_title, playlist_added = sc_client.add_track_to_genre_playlist(track, genre)
            except Exception as e:
                logger.error("Error adding track %s to genre playlist: %s", track_id, e)
                run_had_failures = True

            # Action C: Extract musical key signature
            musical_key = sc_client.extract_musical_key(track)

            # Action D: Notify Telegram (H-2 & V-6 tri-state: ONLY notify if track was newly added and not previously notified)
            telegram_status = "SKIPPED"
            if playlist_added and track_id not in notified_lookup:
                try:
                    telegram_status = telegram.send_track_notification(
                        track=track,
                        playlist_title=playlist_title,
                        artist_follow_status=follow_status,
                        musical_key=musical_key
                    )
                    if telegram_status == "SENT":
                        notified_track_ids.append(track_id)
                        notified_lookup.add(track_id)
                    elif telegram_status == "FAILED":
                        run_had_failures = True
                    # Note: telegram_status == "SKIPPED" (unconfigured telegram) does NOT trigger run_had_failures
                except Exception as e:
                    logger.error("Error sending Telegram notification for track %s: %s", track_id, e)
                    telegram_status = "FAILED"
                    run_had_failures = True

            processed_summary.append({
                "track_id": track_id,
                "title": track_title,
                "genre": genre,
                "playlist": playlist_title,
                "musical_key": musical_key,
                "artist_follow_status": follow_status,
                "playlist_added": playlist_added,
                "telegram_status": telegram_status
            })

        # 5. Update state and persist to GCS
        if Config.STATE_BUCKET:
            if not run_had_failures:
                app_state["last_processed_like_id"] = new_highest_like_id
            else:
                logger.warning("Run encountered processing errors. Keeping last_processed_like_id at %s to allow retrying failed operations on next run.", last_processed_like_id)

            app_state["notified_track_ids"] = notified_track_ids
            app_state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            state_manager.save_state(Config.STATE_BUCKET, app_state)

        return jsonify({
            "status": "success",
            "lookback_minutes": lookback,
            "processed_count": len(processed_summary),
            "tracks": processed_summary
        }), 200

    except Exception as err:
        # M-5: Log error detail server-side, return safe error response to client
        logger.exception("Execution failed: %s", err)
        return jsonify({
            "status": "error",
            "message": "An internal error occurred during execution."
        }), 500

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    return main(flask_request)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
