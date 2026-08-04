import functions_framework
from flask import jsonify, request
from config import Config
from soundcloud_client import SoundCloudClient
from telegram_notifier import TelegramNotifier

@functions_framework.http
def main(request_obj):
    """
    HTTP Cloud Function / Cloud Run entry point.
    Triggered periodically via GCP Cloud Scheduler or HTTP GET/POST.
    """
    # 1. Parse optional query parameters or JSON body for custom lookback
    lookback = Config.LOOKBACK_MINUTES
    
    if request_obj:
        args = request_obj.args or {}
        json_data = request_obj.get_json(silent=True) or {}
        custom_lookback = args.get("lookback_minutes") or json_data.get("lookback_minutes")
        if custom_lookback:
            try:
                lookback = int(custom_lookback)
            except ValueError:
                pass

    # 2. Check essential credentials
    if not Config.SOUNDCLOUD_CLIENT_ID or not Config.SOUNDCLOUD_CLIENT_SECRET or not Config.SOUNDCLOUD_REFRESH_TOKEN:
        return jsonify({
            "status": "error",
            "message": "SoundCloud API credentials (client_id, client_secret, refresh_token) are missing."
        }), 500

    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        print("Warning: Telegram credentials missing. Notifications will be skipped.")

    sc_client = SoundCloudClient(
        client_id=Config.SOUNDCLOUD_CLIENT_ID,
        client_secret=Config.SOUNDCLOUD_CLIENT_SECRET,
        refresh_token=Config.SOUNDCLOUD_REFRESH_TOKEN,
        access_token=Config.SOUNDCLOUD_ACCESS_TOKEN,
        playlist_prefix=Config.PLAYLIST_PREFIX,
        default_genre=Config.DEFAULT_GENRE
    )

    telegram = TelegramNotifier(
        bot_token=Config.TELEGRAM_BOT_TOKEN,
        chat_id=Config.TELEGRAM_CHAT_ID
    )

    processed_summary = []

    try:
        # 3. Fetch recent likes within lookback window
        recent_tracks = sc_client.get_recent_likes(lookback_minutes=lookback)
        print(f"Found {len(recent_tracks)} liked tracks in the last {lookback} minutes.")

        for track in recent_tracks:
            track_id = track.get("id")
            track_title = track.get("title", "Unknown Track")
            artist_id = track.get("user", {}).get("id")
            genre = track.get("genre") or Config.DEFAULT_GENRE

            # Action A: Follow the artist
            artist_followed = False
            if artist_id:
                try:
                    artist_followed = sc_client.follow_artist(artist_id)
                except Exception as e:
                    print(f"Error following artist {artist_id}: {e}")

            # Action B: Add track to genre playlist (or create playlist)
            playlist_title = f"{Config.PLAYLIST_PREFIX}{genre}"
            playlist_added = False
            try:
                playlist_title, playlist_added = sc_client.add_track_to_genre_playlist(track, genre)
            except Exception as e:
                print(f"Error adding track {track_id} to genre playlist: {e}")

            # Action C: Extract musical key signature
            musical_key = sc_client.extract_musical_key(track)

            # Action D: Notify Telegram
            telegram_sent = False
            try:
                telegram_sent = telegram.send_track_notification(
                    track=track,
                    playlist_title=playlist_title,
                    artist_followed=artist_followed,
                    musical_key=musical_key
                )
            except Exception as e:
                print(f"Error sending Telegram notification for track {track_id}: {e}")

            processed_summary.append({
                "track_id": track_id,
                "title": track_title,
                "genre": genre,
                "playlist": playlist_title,
                "musical_key": musical_key,
                "artist_followed": artist_followed,
                "telegram_notified": telegram_sent
            })

        return jsonify({
            "status": "success",
            "lookback_minutes": lookback,
            "processed_count": len(processed_summary),
            "tracks": processed_summary
        }), 200

    except Exception as err:
        print(f"Execution failed: {err}")
        return jsonify({
            "status": "error",
            "message": str(err)
        }), 500

if __name__ == "__main__":
    import os
    # For local execution testing
    from flask import Flask, request as flask_request
    app = Flask(__name__)
    @app.route("/", methods=["GET", "POST"])
    def index():
        return main(flask_request)
    
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
