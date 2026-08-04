import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("soundcloud_automation")
_STATE_OBJECT_NAME = "state.json"

def load_state(bucket_name: str) -> Dict[str, Any]:
    """
    Loads persistent state dictionary from GCS bucket.
    Returns empty dict on first run or if state object is missing/corrupt.
    """
    if not bucket_name:
        return {}

    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(_STATE_OBJECT_NAME)
        
        if not blob.exists():
            logger.info("No state object found in bucket '%s'. Starting fresh.", bucket_name)
            return {}

        content = blob.download_as_bytes()
        state = json.loads(content)
        logger.info("Successfully loaded state from bucket '%s'. Last processed ID: %s",
                    bucket_name, state.get("last_processed_like_id"))
        return state
    except Exception as e:
        logger.warning("Could not load state from GCS bucket '%s': %s. Treating as first run.", bucket_name, e)
        return {}

def save_state(bucket_name: str, state: Dict[str, Any]) -> None:
    """
    Saves state dictionary to GCS bucket.
    Caps notified_track_ids to last 500 entries (FIFO).
    """
    if not bucket_name:
        return

    # FIFO cap notified_track_ids to max 500 entries
    if "notified_track_ids" in state and isinstance(state["notified_track_ids"], list):
        state["notified_track_ids"] = state["notified_track_ids"][-500:]

    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(_STATE_OBJECT_NAME)
        blob.upload_from_string(json.dumps(state, indent=2), content_type="application/json")
        logger.info("Successfully saved updated state to bucket '%s'.", bucket_name)
    except Exception as e:
        logger.warning("Failed to save state to GCS bucket '%s': %s", bucket_name, e)
