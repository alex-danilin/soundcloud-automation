# Remaining Work — Implementation Instructions

Derived from `CODE_REVIEW.md` after Verification Pass 4 (commit `8ca89b0`). Covers every open item **except M-15 (CI workflow)**, which is deliberately out of scope here.

**Nothing below is a blocker.** All 5 critical, all 8 high (bar H-7's concurrency half), and 11 of 15 medium findings are already fixed and verified. This is hardening and cleanup.

Work the tasks in the order given. Tasks 1–7 are independent of each other; **task 8 (H-7) is last on purpose** — it is the only invasive change and it benefits from everything else being stable first.

| # | Item | Effort | Risk | Value |
|---|---|---|---|---|
| 1 | M-13 — remote Terraform backend | ~30 min | Low | **Highest** |
| 2 | M-12 — hash-pinned dependencies | ~20 min | Low | High |
| 3 | N-1 — state load/save must fail loudly | ~20 min | Low | High |
| 4 | L-4 — proactive token expiry | ~15 min | Low | Medium |
| 5 | L-6 — Cloud Run startup probe | ~10 min | Low | Medium |
| 6 | L-5 — align Python versions | ~10 min | Low | Low |
| 7 | N-2 / N-3 — Flask app scope, dotenv placement | ~10 min | Low | Low |
| 8 | H-7 — bounded concurrency | ~2 h | **Medium-High** | Low |

After every task: `python -m unittest test_app` must stay at **11 passed**, and `cd terraform && terraform validate` must stay **Success**.

---

## 1. M-13 — Move Terraform state to a remote GCS backend

**Why.** `terraform.tfstate` currently sits on disk in the working directory and contains all five credentials **in plaintext**. The `sensitive = true` markers in `variables.tf` only suppress CLI output; they do not encrypt state. `.gitignore` keeps it out of git, which is the only control in place today. A remote backend adds encryption at rest, versioning, access control, and state locking.

**Chicken-and-egg warning.** The bucket that *holds* state cannot be managed by the configuration that *uses* it. Create it out-of-band.

### 1a. Create the state bucket

```bash
PROJECT_ID=<your-project-id>
REGION=us-central1

gcloud storage buckets create "gs://${PROJECT_ID}-tfstate" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --uniform-bucket-level-access \
  --public-access-prevention

gcloud storage buckets update "gs://${PROJECT_ID}-tfstate" --versioning
```

### 1b. Add the backend block

In `terraform/main.tf`, inside the existing `terraform { ... }` block (currently lines 1–13), add:

```hcl
terraform {
  required_version = ">= 1.3.0"

  backend "gcs" {
    bucket = "REPLACE-WITH-PROJECT-ID-tfstate"
    prefix = "soundcloud-bot"
  }

  required_providers { ... }   # leave unchanged
}
```

**Backend config cannot use variables or interpolation** — `var.project_id` will fail with `Variables not allowed`. Either hardcode the literal bucket name, or use partial configuration:

```hcl
backend "gcs" {}                       # in main.tf
```
```hcl
# terraform/backend.hcl — safe to commit, contains no secrets
bucket = "my-project-tfstate"
prefix = "soundcloud-bot"
```
```bash
terraform init -backend-config=backend.hcl -migrate-state
```

### 1c. Migrate and scrub

```bash
cd terraform
terraform init -migrate-state      # answer "yes" to copy existing state
terraform state list               # confirm all resources are present
terraform plan                     # MUST report "No changes"
```

Then destroy the plaintext local copies — they still contain every credential:

```bash
rm -f terraform.tfstate terraform.tfstate.backup
rm -rf .terraform
```

> On a shared or recoverable filesystem, overwrite rather than unlink (`shred -u` on Linux, `sdelete` on Windows). Also rotate the credentials afterward if the local state was ever on a synced/backed-up drive.

### 1d. Lock the bucket down

Grant `roles/storage.objectAdmin` on the tfstate bucket **only** to the humans and service accounts that run Terraform. Do not grant it to `runtime_sa` — that account already has its own separate state bucket for application data, and the two must never be confused.

Optionally add CMEK. If you do, you **must** first grant the GCS service agent access to the key, or bucket creation fails:

```bash
gcloud storage service-agent --project="${PROJECT_ID}"      # prints the agent address
gcloud kms keys add-iam-policy-binding <KEY> \
  --keyring=<RING> --location="${REGION}" \
  --member="serviceAccount:<gcs-service-agent>" \
  --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

**Verify:** `terraform plan` reports no changes; `gcloud storage ls gs://${PROJECT_ID}-tfstate/soundcloud-bot/` shows `default.tfstate`; no `*.tfstate` remains in `terraform/`.

---

## 2. M-12 — Hash-pin dependencies

**Why.** Every entry uses `>=` with an upper bound but no exact pin, no lockfile, and no hashes. Builds are not reproducible, and a compromised release of any transitive dependency lands in production on the next image build.

### 2a. Convert to `.in` sources

```bash
git mv requirements.txt requirements.in
```

Create `requirements-dev.in` — note it **references** the runtime file rather than duplicating it. The current `requirements-dev.txt` copies all seven runtime lines verbatim, which will silently drift:

```
-r requirements.in
pytest>=7.0.0
pytest-cov>=4.0.0
```

### 2b. Compile locks

```bash
pip install pip-tools
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in
```

Commit all four files (`.in` sources and generated `.txt` locks).

### 2c. Enforce at build time

In `Dockerfile`, change line 14:

```dockerfile
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
```

`--require-hashes` forces every dependency — including transitive ones — to be pinned with a hash. `pip-compile --generate-hashes` produces exactly that. If the build fails with `Hashes are required`, a dependency is missing from the lock; re-run `pip-compile`.

### 2d. Keep it fresh

Since versions are now frozen, add a `.github/dependabot.yml` (or Renovate) targeting `requirements.in` so upgrades arrive as reviewable PRs rather than silently. *(Config only — no CI workflow, which stays out of scope per M-15.)*

**Verify:** `docker build -t sc-test .` succeeds; `pip install --require-hashes -r requirements.txt` succeeds in a clean venv; tests still pass.

---

## 3. N-1 — State load/save failures must fail loudly

**Why.** `state_manager.py` currently swallows every error on both paths:

- **`load_state` (lines 39–41)** returns `{}` on *any* exception. A transient GCS error is therefore indistinguishable from a first run — the marker resets to `None` and the ledger empties, so the app reprocesses the entire like history and re-sends notifications for up to 500 tracks. This is the exact H-2 duplicate-notification failure, reachable from one 503.
- **`save_state` (lines 65–66)** logs a warning and continues, so the run returns HTTP 200 while the marker was never persisted.
- **The `ImportError` shim (lines 5–8)** sets `storage = None`, converting a missing library into the same silent "first run" path. This is the shape V-8 removed from `main.py`; it survived here.

### Changes to `state_manager.py`

Replace the shim with a direct import — the package is pinned in both requirements files, so a missing library is a broken image and should crash at startup:

```python
import json
import logging
from typing import Dict, Any

from google.cloud import storage
from google.api_core import exceptions as gcp_exceptions

logger = logging.getLogger("soundcloud_automation")
_STATE_OBJECT_NAME = "state.json"
```

Distinguish "object absent" (a genuine first run) from "everything else" (a real fault):

```python
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
            "Refusing to continue — fix or delete gs://%s/%s to force a first run."
            % (bucket_name, _STATE_OBJECT_NAME)
        ) from e

    if not isinstance(state, dict):
        raise RuntimeError(f"State object in '{bucket_name}' is not a JSON object.")

    logger.info("Loaded state from '%s'. Last processed ID: %s",
                bucket_name, state.get("last_processed_like_id"))
    return state
```

A corrupt object raising (rather than resetting) is deliberate: bucket versioning is enabled, so recovery is a rollback, and a silent reset costs 500 duplicate messages.

Make save failures loud:

```python
def save_state(bucket_name: str, state: Dict[str, Any]) -> None:
    if not bucket_name:
        return

    if isinstance(state.get("notified_track_ids"), list):
        state["notified_track_ids"] = state["notified_track_ids"][-500:]

    try:
        blob = storage.Client().bucket(bucket_name).blob(_STATE_OBJECT_NAME)
        blob.upload_from_string(json.dumps(state, indent=2), content_type="application/json")
        logger.info("Saved state to '%s'.", bucket_name)
    except Exception as e:
        logger.error("FAILED to persist state to '%s': %s. "
                     "Next run will reprocess this window.", bucket_name, e)
        raise
```

`main.py`'s outer handler (line 246) already catches this and returns 500, so Cloud Scheduler will retry — which is the correct outcome, since a run whose state did not persist has not really succeeded.

### Test updates

`test_state_manager_fifo_capping` patches `state_manager.storage`; that still works with a direct import. Add two cases:

- `load_state` raises `RuntimeError` when the blob body is not valid JSON
- `load_state` returns `{}` when `download_as_bytes` raises `NotFound`

**Verify:** tests pass; `grep -c "except ImportError" state_manager.py` returns 0.

---

## 4. L-4 — Track token expiry instead of waiting for 401

**Why.** `ensure_access_token` ignores `expires_in` from the token response, so the client only discovers expiry by receiving a 401 and retrying (`soundcloud_client.py:101-105`). That wastes one request per expiry and makes every first-call-after-expiry slower. The reactive path is a correct backstop; it should not be the primary mechanism.

### Changes to `soundcloud_client.py`

In `__init__`, add:

```python
self._token_expires_at: Optional[datetime.datetime] = None
```

Add a helper and use it as the cache guard:

```python
_EXPIRY_SAFETY_MARGIN_SECONDS = 60

def _token_is_fresh(self) -> bool:
    if not self.access_token:
        return False
    if self._token_expires_at is None:
        return True          # externally supplied token: expiry unknown, rely on the 401 path
    return datetime.datetime.now(datetime.timezone.utc) < self._token_expires_at
```

Change the early return at the top of `ensure_access_token`:

```python
if self._token_is_fresh() and not force_refresh:
    return self.access_token
```

After parsing the response (near line 72), record expiry:

```python
self.access_token = data.get("access_token", "")

expires_in = data.get("expires_in")
if expires_in:
    try:
        self._token_expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=int(expires_in) - _EXPIRY_SAFETY_MARGIN_SECONDS)
        )
    except (ValueError, TypeError):
        self._token_expires_at = None
        logger.warning("Unparseable expires_in from token endpoint: %r", expires_in)
else:
    self._token_expires_at = None
```

**Keep the 401 retry in `_make_request` unchanged.** Clock skew and server-side revocation both exist; proactive refresh is an optimisation, not a replacement.

Note the `_token_expires_at is None → fresh` branch: `SOUNDCLOUD_ACCESS_TOKEN` supplied via env has unknown expiry, so behaviour there is exactly what it is today.

**Add tests:** a fresh token is reused without an HTTP call; an expired token triggers exactly one refresh; a token with no `expires_in` still works.

---

## 5. L-6 — Add a Cloud Run startup probe (not a Dockerfile `HEALTHCHECK`)

**Why the original suggestion was wrong for this platform.** `HEALTHCHECK` in a Dockerfile is **ignored by Cloud Run** — it runs its own probes. Adding one would be decoration. The platform-correct equivalent is a probe on the service.

Cloud Run already applies a default TCP startup probe (does the container listen on `$PORT`), so the gap is small. An explicit probe buys faster, more predictable failure detection on a bad revision.

In `terraform/main.tf`, inside `google_cloud_run_v2_service.default` → `template` → `containers`, add alongside `resources`:

```hcl
      ports {
        container_port = 8080
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 5
        failure_threshold     = 3
      }
```

Use `tcp_socket`, **not** `http_get`. Under `functions-framework --target=main`, every path routes to the single function, so an HTTP probe would invoke the whole SoundCloud sync on each check — hammering the API and burning quota.

Only add a Dockerfile `HEALTHCHECK` if you also intend to run this image outside Cloud Run (Compose, a VM, Kubernetes). Otherwise skip it.

**Verify:** `terraform validate` → Success; `terraform plan` shows only the probe/ports addition.

---

## 6. L-5 — Align local and container Python versions

**Why.** The container is `python:3.11-slim` (`Dockerfile:1`); local tests run 3.14. `datetime.fromisoformat` leniency differs materially between those versions, so a green local suite does not prove container behaviour. Impact is currently low — the timestamp branch only executes for wrapped like-events, which the live API does not return — but the drift undermines every test result.

Pick **one**:

**Option A (recommended) — pin local to the container version.** Add `.python-version`:

```
3.11
```

and a line in `README.md` under Prerequisites: *"Local development requires Python 3.11 to match the container (`pyenv install 3.11`)."*

**Option B — bump the container.** Change `Dockerfile:1` to `FROM python:3.12-slim`, then rebuild and re-run the suite inside the image:

```bash
docker build -t sc-test . && docker run --rm --entrypoint python sc-test -m unittest discover
```

Do **not** jump to 3.14 in the container — `functions-framework` and the `google-cloud-*` wheels should be confirmed available for the target version first.

### Optional hardening

Remove the dependence on interpreter leniency entirely by parsing SoundCloud's documented format explicitly before falling back:

```python
for fmt in ("%Y/%m/%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
    try:
        liked_dt = datetime.datetime.strptime(str(created_at_str), fmt)
        break
    except ValueError:
        continue
else:
    liked_dt = datetime.datetime.fromisoformat(normalised)   # last resort
```

---

## 7. N-2 / N-3 — Flask app scope and dotenv placement

### N-2. Stop creating a production Flask app that never serves traffic

`main.py:254-258` instantiates a module-level `Flask(__name__)` and registers `/`. In production the container runs `functions-framework --target=main`, which builds **its own** app and calls `main` directly — so this second app and its route are dead weight created on every import. It exists only to give tests an `app_context()` for `jsonify`.

Move it back under the entrypoint guard:

```python
if __name__ == "__main__":
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        return main(flask_request)

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
```

Then have the test build its own context instead of importing `app`. In `test_app.py`, replace `from main import main, app` and `with app.app_context():` with:

```python
import flask

# module level in the test file
_TEST_APP = flask.Flask(__name__)

# in the test
with _TEST_APP.app_context():
    resp_data, status_code = main(mock_req)
```

The test still exercises the real `flask.jsonify`, so the V-8 fix is preserved — it just no longer requires production code to keep an unused app alive.

### N-3. Move `python-dotenv` to dev dependencies

`load_dotenv()` only matters locally; in Cloud Run every value arrives as an env var, so in production the call is a no-op on a package that is nonetheless installed in the image.

- Remove `python-dotenv>=1.0.0,<2.0.0` from `requirements.in`
- Add `python-dotenv>=1.0.0,<2.0.0` to `requirements-dev.in`
- Re-run both `pip-compile` commands from task 2

**Keep the `try/except ImportError` at `config.py:3-7`.** Once dotenv is dev-only, that guard becomes load-bearing and correct — unlike the shims removed in V-8 and N-1, it guards an intentionally-absent optional dependency rather than masking a broken install. Add a comment saying so, so a future reader does not "clean it up":

```python
# python-dotenv is a DEV-ONLY dependency (see requirements-dev.in). Absent in the
# production image by design — Cloud Run supplies configuration as env vars.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
```

---

## 8. H-7 — Bounded concurrency for the per-track loop

**Read this section fully before starting.** It is the only change here that can introduce data loss, and its value is now much lower than when it was first raised.

**Why the urgency dropped.** H-7's original risk was a 50-track run blowing the 180 s scheduler deadline. Since then: `attempt_deadline` and the service `timeout` are both 600 s, `max_instance_count = 2`, and — most importantly — V-1's resume marker plus the notified ledger mean a steady-state run processes only *genuinely new* likes, typically a handful. The sequential loop is no longer the bottleneck it was.

**Consider stopping here.** If you do not need the speedup, the correct action is to record the decision, not to half-build it. Add to `main.py` above the loop:

```python
# H-7: intentionally sequential. Playlist PUTs are read-modify-write and must be
# serialised per playlist; the resume marker + notified ledger keep each run small
# enough that the 600s budget is ample. See CODE_REVIEW.md H-7 before parallelising.
```

If you do proceed, all five hazards below **must** be handled. Parallelising naively will silently delete tracks from playlists.

### Hazard 1 — Playlist writes are read-modify-write (would cause data loss)

`add_track_to_genre_playlist` does GET-authoritative-tracks → PUT-full-list. Two threads adding tracks to the *same* genre playlist will both read the pre-state and the second PUT will overwrite the first — reintroducing C-5, the data-loss bug, by a different route.

Add per-playlist locking in `soundcloud_client.py`:

```python
import threading

# in __init__
self._playlist_locks: Dict[str, threading.Lock] = {}
self._playlist_locks_guard = threading.Lock()

def _lock_for_playlist(self, title: str) -> threading.Lock:
    with self._playlist_locks_guard:
        return self._playlist_locks.setdefault(title, threading.Lock())
```

Wrap the entire body of `add_track_to_genre_playlist` after `playlist_title` is computed:

```python
with self._lock_for_playlist(playlist_title):
    ...   # existing body unchanged
```

### Hazard 2 — Token refresh mutates shared state

`ensure_access_token` writes `self.access_token`, `self.refresh_token`, and fires the Secret Manager callback. N threads hitting 401 simultaneously would trigger N refreshes and N secret versions.

Serialise with a lock plus a token-generation check, so threads that were waiting return the token another thread already fetched:

```python
# in __init__
self._token_lock = threading.Lock()

def ensure_access_token(self, force_refresh: bool = False, seen_token: Optional[str] = None) -> str:
    with self._token_lock:
        if self._token_is_fresh() and not force_refresh:
            return self.access_token
        # Another thread already refreshed past the token this caller saw fail.
        if force_refresh and seen_token and self.access_token != seen_token:
            return self.access_token
        ...   # existing refresh body
```

And in `_make_request`, pass the token that got the 401:

```python
stale = headers.get("Authorization", "").removeprefix("OAuth ")
token = self.ensure_access_token(force_refresh=True, seen_token=stale)
```

### Hazard 3 — Pre-warm the playlist cache

`get_user_playlists` populates `_playlists_cache` lazily; concurrent first-callers would each paginate the full list. Populate it once, single-threaded, before submitting any work:

```python
sc_client.get_user_playlists()      # warm the cache before the pool starts
```

### Hazard 4 — The notified ledger is a check-then-act race

`if playlist_added and track_id not in notified_lookup:` … later `notified_lookup.add(track_id)` is not atomic; two threads could both send for the same ID. Guard both sides in `main.py`:

```python
ledger_lock = threading.Lock()

with ledger_lock:
    already_notified = track_id in notified_lookup
if playlist_added and not already_notified:
    status = telegram.send_track_notification(...)
    if status == "SENT":
        with ledger_lock:
            notified_track_ids.append(track_id)
            notified_lookup.add(track_id)
```

`run_had_failures = True` needs no lock — under the GIL a plain bool store is atomic and the value is write-once-True.

### Hazard 5 — Connection pool sizing and result ordering

Raise the adapter pool to at least `max_workers` or threads will serialise on connections, negating the change:

```python
adapter = HTTPAdapter(max_retries=retries, pool_connections=8, pool_maxsize=8)
```

`processed_summary.append` from threads is safe but yields nondeterministic order. Collect `(index, summary)` and sort before returning, so the HTTP response stays stable.

### Wiring, behind a flag

Default to `1` so deploying this change is a no-op until deliberately turned on.

`config.py`:
```python
MAX_WORKERS = max(1, min(_parse_int(os.getenv("MAX_WORKERS", "1"), 1), 8))
```

`terraform/variables.tf`:
```hcl
variable "max_workers" {
  type        = string
  description = "Concurrent track workers (1 = sequential)"
  default     = "1"

  validation {
    condition     = can(regex("^[1-8]$", var.max_workers))
    error_message = "max_workers must be an integer from 1 to 8."
  }
}
```

Add the matching `env` block to the service, then in `main.py` extract the loop body into `_process_track(track) -> dict` and dispatch:

```python
if Config.MAX_WORKERS > 1:
    with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as pool:
        results = list(pool.map(_process_track, recent_tracks))
else:
    results = [_process_track(t) for t in recent_tracks]
```

Keep `new_highest_like_id = recent_tracks[0]["id"]` computed **before** dispatch — it must not depend on completion order.

### Verification

Extend the table-driven test with concurrency cases at `MAX_WORKERS=4`:

- 20 tracks all in the **same** genre → assert `add_track_to_genre_playlist` never interleaves for one playlist, and the final tracklist contains all 20
- one track fails → marker still held (V-5 must survive)
- the same track ID twice in one batch → exactly one notification (Hazard 4)

Then rate-limit sanity: 4 workers × 3 calls per track can trip SoundCloud's limits. Confirm the H-3 retry config absorbs 429s before raising `MAX_WORKERS` in production, and start at 2.

---

## Definition of done

- [ ] `python -m unittest test_app` → 11+ passed
- [ ] `cd terraform && terraform validate` → Success, `terraform fmt -check` → clean
- [ ] `terraform plan` → only intended changes
- [ ] `docker build .` succeeds with `--require-hashes`
- [ ] No `*.tfstate` in the repo working tree; remote state confirmed in GCS
- [ ] `grep -rn "except ImportError" *.py` → only `config.py`, with its explanatory comment
- [ ] `CODE_REVIEW.md` updated: M-12, M-13, L-4, L-5, L-6, N-1, N-2, N-3 marked resolved; H-7 marked resolved **or** explicitly deferred with the code comment in place
