import json
import logging
from typing import Dict, Any

from google.cloud import storage
from google.api_core import exceptions as gcp_exceptions

logger = logging.getLogger("soundcloud_automation")
_STATE_OBJECT_NAME = "state.json"

def load_state(bucket_name: str) -> Dict[str, Any]:
    """
    Loads persistent state from GCS.
    Returns {} ONLY when state is legitimately absent (first run) or no bucket is configured.
    Raises on any other fault — a failed load must never masquerade as a first run,
    because that resets the resume marker and re-notifies up to 500 tracks.
    """
    if not bucket_name:
        return {}

    blob = storage.Client().bucket(bucket_name).blob(_STATE_OBJECT_NAME)
    try:
        content = blob.download_as_bytes()
    except gcp_exceptions.NotFound:
        logger.info("No state object in bucket '%s'. Treating as first run.", bucket_name)
        return {}

    try:
        state = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"State object in '{bucket_name}' is corrupt: {e}. "
            f"Refusing to continue — fix or delete gs://{bucket_name}/{_STATE_OBJECT_NAME} to force a first run."
        ) from e

    if not isinstance(state, dict):
        raise RuntimeError(f"State object in '{bucket_name}' is not a JSON object.")

    logger.info("Loaded state from '%s'. Last processed ID: %s",
                bucket_name, state.get("last_processed_like_id"))
    return state

def save_state(bucket_name: str, state: Dict[str, Any]) -> None:
    """
    Saves state dictionary to GCS bucket.
    Caps notified_track_ids to last 500 entries (FIFO).
    Raises on error so execution fails loudly.
    """
    if not bucket_name:
        return

    if isinstance(state.get("notified_track_ids"), list):
        state["notified_track_ids"] = state["notified_track_ids"][-500:]

    try:
        blob = storage.Client().bucket(bucket_name).blob(_STATE_OBJECT_NAME)
        blob.upload_from_string(json.dumps(state, indent=2), content_type="application/json")
        logger.info("Saved state to '%s'.", bucket_name)
    except Exception as e:
        logger.error("FAILED to persist state to '%s': %s. Next run will reprocess this window.", bucket_name, e)
        raise
