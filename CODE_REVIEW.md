# Code Review — SoundCloud Auto Follow & Genre Playlist Bot

**Date:** 2026-08-04
**Reviewed revision:** working tree at `main` (6 files modified vs. `4edef2e`)
**Scope:** `main.py`, `soundcloud_client.py`, `telegram_notifier.py`, `config.py`, `test_app.py`, `Dockerfile`, `requirements.txt`, `terraform/*`
**Test status:** `python -m unittest test_app` → **8 passed, 0 failed**

---

## Executive Summary

The codebase is small, readable, and shows real engineering care in places — connection pooling, HTML escaping, timezone-aware datetimes, IAM-authenticated scheduler invocation, and secrets kept out of source control. The recent commit (`4edef2e`) fixed several genuine issues.

However, **the service as written will not do its job in production.** There are three independent blockers:

1. **It filters likes by the wrong timestamp** — it uses the track's *upload* date, not the *like* date, so on a typical hourly run it processes nothing.
2. **It cannot deploy** — Terraform never grants the runtime service account access to Secret Manager, and never builds or pushes the container image the Cloud Run service references.
3. **It cannot survive token rotation** — the refresh-token persistence path is dead code on Cloud Run, so the first rotation permanently breaks authentication.

There is also a **silent data-loss defect**: the playlist update rewrites the entire tracklist from a possibly-truncated local copy, which can delete tracks from the user's playlists.

| Area | Rating | Notes |
|---|---|---|
| Correctness | ⚠️ **Poor** | Core recency filter is wrong; data-loss bug in playlist update |
| Security | 🟡 **Fair** | Good secrets hygiene & IAM auth; root container, unpinned deps, local state with plaintext secrets |
| Performance | 🟡 **Fair** | Good connection reuse; no pagination, O(n) playlist rewrites, timeout risk |
| Reliability | ⚠️ **Poor** | Retries don't cover POST; duplicate notifications by design; no idempotency state |
| Deployability | ❌ **Broken** | Missing IAM binding and image build; README overstates automation |
| Testing | 🟡 **Fair** | 8 focused tests, all passing; zero coverage of `main.py`, auth, or error paths |

**Findings:** 5 critical · 8 high · 15 medium · 8 low

---

## Critical (P0) — blocks correct operation

### C-1. Recency filter uses track upload time, not like time
`soundcloud_client.py:129`

```python
created_at_str = item.get("created_at") or track.get("created_at")
```

`GET /me/likes/tracks` returns **bare track objects**, not like-event wrappers. So `item` *is* the track, and `item.get("created_at")` resolves to the track's **upload** timestamp. Line 124 (`item.get("track", item) if ... "track" in item`) confirms the author expected a wrapper that this endpoint does not return.

**Consequence:** with the default 65-minute window, the service only processes tracks that were *uploaded* in the last 65 minutes. Like a track released last week and it is silently skipped — the primary feature of the app never fires. The unit test passes only because it mocks a wrapper shape (`{"created_at": ..., "track": {...}}`) the real API never sends, so the test actively conceals the bug.

**Fix:** the public SoundCloud API exposes no per-like timestamp on this endpoint. Options, in order of preference:
- Persist the highest-seen like position/track ID between runs in a single GCS object and diff against it — see [§6 of the addendum](#6-the-state-layer-concretely) for the concrete design. This also fixes C-4 and H-2.
- Fetch the first page of likes and treat *order* as recency (the endpoint returns most-recently-liked first), processing everything above the last-seen marker.
- If a stateless design is a hard requirement, drop the time filter and rely solely on the playlist-membership check for dedupe — but then Telegram notifications must be gated on `playlist_added` (see H-2).

Whatever you choose, **replace the mocked wrapper shape in `test_app.py:55-60` with a real `/me/likes/tracks` response payload.**

---

### C-2. Terraform never grants Secret Manager access → service cannot start
`terraform/main.tf:57-139`

The Cloud Run service mounts five secrets via `value_source.secret_key_ref`, but the configuration contains **no `google_secret_manager_secret_iam_member`** and **no `service_account` on the `template` block**. Verified:

```
$ grep -rn "service_account|secretAccessor|iam_member" terraform/
main.tf:142  google_service_account.invoker_sa      # scheduler invoker only
main.tf:148  google_cloud_run_v2_service_iam_member # run.invoker only
```

The revision therefore runs as the **default compute service account**, which must hold `roles/secretmanager.secretAccessor`. On projects created since the `constraints/iam.automaticIamGrantsForDefaultServiceAccounts` org policy became default-enforced, that SA has no project roles at all, and the revision fails to start with `Permission denied on secret`.

**Fix:**

```hcl
resource "google_service_account" "runtime_sa" {
  account_id   = "${var.service_name}-runtime-sa"
  display_name = "SoundCloud Bot Runtime Service Account"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = local.secrets_map
  secret_id = google_secret_manager_secret.secrets[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime_sa.email}"
}

# inside google_cloud_run_v2_service.default.template:
#   service_account = google_service_account.runtime_sa.email
# and add google_secret_manager_secret_iam_member.accessor to depends_on
```

This also fixes the least-privilege violation (M-9): the runtime SA should be distinct from the invoker SA and hold nothing but `secretAccessor`.

---

### C-3. Deployment cannot succeed as documented — no image build
`terraform/main.tf:64`, `README.md:53,106-111`

```hcl
image = "gcr.io/${var.project_id}/${var.service_name}:latest"
```

Nothing in the repository builds or pushes this image. `cloudbuild.googleapis.com` is enabled at `main.tf:21` but never used — there is no `google_cloudbuild_trigger`, no `null_resource` with a `gcloud builds submit`, no Artifact Registry repository, and no CI workflow (`.github/` does not exist). The first `terraform apply` fails with `Image not found`.

Meanwhile the README asserts:
> "Deployment is managed entirely via **Terraform**. No manual GCP console steps or `gcloud secrets` commands are required." — `README.md:53`
> "**Complete zero-manual-step deployment.**" — `README.md:19`

**Fix:** either add the build to Terraform, or correct the README. Minimum viable:

```hcl
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

resource "null_resource" "build_push" {
  triggers = { src = sha1(join("", [for f in fileset("${path.module}/..", "*.py") : filesha1("${path.module}/../${f}")])) }
  provisioner "local-exec" {
    command = "gcloud builds submit ${path.module}/.. --tag ${var.region}-docker.pkg.dev/${var.project_id}/${var.service_name}/app:latest --project ${var.project_id}"
  }
}
```

Also note `gcr.io` is legacy; new projects should use `${region}-docker.pkg.dev`. And pin the image by digest rather than `:latest` so revisions are reproducible.

---

### C-4. Refresh-token rotation is dead code on Cloud Run → permanent auth failure
`main.py:14`

```python
project_id = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
```

**Neither variable is set by Cloud Run.** `GCP_PROJECT` was a Cloud Functions *gen 1* variable; `GOOGLE_CLOUD_PROJECT` is set by App Engine and gen-1 functions, not by Cloud Run v2. So `project_id` is `None`, the `if` at `main.py:17` never fires, and the rotated refresh token is discarded — silently, with no log line, because the `print` at line 25 and the warning at 27 are both inside the dead branch.

SoundCloud's OAuth implementation **does rotate refresh tokens** on use. The moment it does, the value in Secret Manager becomes stale and every subsequent run fails at `ensure_access_token` with a 401 — the service dies permanently and only a manual re-auth recovers it.

Compounding this, even if the write worked it would need `roles/secretmanager.secretVersionAdder`, which C-2 shows is not granted.

**Fix:**

```python
def _project_id():
    for k in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "PROJECT_ID"):
        if os.environ.get(k):
            return os.environ[k]
    try:  # Cloud Run: metadata server is the reliable source
        return requests.get(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"}, timeout=2,
        ).text
    except Exception:
        return None
```

Better still: set `PROJECT_ID` explicitly as a plain `env` block in `main.tf` alongside `LOOKBACK_MINUTES` — no metadata call needed, and it is testable. Grant the runtime SA `secretVersionAdder` on `soundcloud-refresh-token` only. **And add a loud `print`/log when persistence is skipped** — a silently-skipped rotation is exactly the failure that took this long to notice.

---

### C-5. Playlist update overwrites the full tracklist → silent data loss
`soundcloud_client.py:214-231`

```python
existing_ids = [...]                                   # from a local/cached copy
updated_track_objs = [{"id": tid} for tid in existing_ids + [track_id]]
payload = {"playlist": {"tracks": updated_track_objs}}
res = self._make_request("PUT", update_url, json=payload, timeout=15)
```

SoundCloud's playlist `PUT` is a **replace**, not an append. The code builds the replacement list from `target_playlist.get("tracks")` — a value taken from the `/me/playlists` list response or from the in-process cache. That is unsafe for three reasons:

1. **`/me/playlists` truncates the `tracks` array.** The guard at line 204 only refetches when `tracks` is `None`; a *present-but-truncated* array passes straight through. Reproduced:

   ```
   cache: {'id': 9, 'title': 'Genre: Techno', 'tracks': [{'id': 11}, {'id': 12}]}   # server has 500
   PUT payload: {'playlist': {'tracks': [{'id': 11}, {'id': 12}, {'id': 13}]}}      # 497 tracks deleted
   ```

2. **The cache is never invalidated within a run** (`_playlists_cache`, line 30), so any concurrent change — the user editing the playlist in the app, or an overlapping invocation — is overwritten.

3. **Payload grows linearly.** At SoundCloud's 500-track playlist ceiling, every single-track add ships ~500 objects. That is O(n) bandwidth and JSON serialization per add, and O(n²) across a filling playlist.

**Fix:** always fetch authoritative playlist state immediately before the write, and prefer an append-style API if available:

```python
if playlist_id:
    pl_resp = self._make_request("GET", f"{self.BASE_URL}/playlists/{playlist_id}", timeout=15)
    if not pl_resp.ok:
        raise RuntimeError(f"Cannot read playlist {playlist_id} before update: {pl_resp.status_code}")
    existing_tracks = pl_resp.json().get("tracks", [])
```

Remove the `existing_tracks is not None` shortcut entirely — the extra GET costs one round trip; the bug costs the user's playlists. Also guard against the 500-track limit and surface a clear error instead of a failed `PUT`.

---

## High (P1)

### H-1. `PUT /playlists/None` when the create endpoint returns an empty body
`soundcloud_client.py:263`

```python
created_playlist = res.json() if res.text else {"id": None, "title": playlist_title, "tracks": [...]}
```

That `id: None` placeholder is appended to `_playlists_cache` (line 265). The next track with the same genre in the same run matches it by title and issues a request to a literal `None` URL. Reproduced:

```
A1: ('Genre: Techno', True)
A2: ('Genre: Techno', True)     # <- reports success
    POST https://api.soundcloud.com/playlists           {'playlist': {...'tracks': [{'id': 1}]}}
    PUT  https://api.soundcloud.com/playlists/None      {'playlist': {'tracks': [{'id': 1}, {'id': 2}]}}
```

Note the second call **returns `True`** (added) while having done nothing. The user is notified that a track was filed into a playlist it never reached.

**Fix:** treat a create response without a usable `id` as a hard failure — `raise RuntimeError`, do not cache the placeholder. Add an `if not playlist_id: raise` guard before every update path.

### H-2. Guaranteed duplicate Telegram notifications
`config.py:22`, `main.py:108-117`

`LOOKBACK_MINUTES` defaults to **65** while the cron is **hourly** (`main.tf:159`, `"0 * * * *"`). The 5-minute overlap is deliberate (avoids gaps from clock skew), but there is no dedupe state, so every track liked in that window is processed **twice**. The playlist add correctly returns `(title, False)` on the second pass — but `main.py:110` sends the Telegram notification unconditionally, ignoring `playlist_added`. Cloud Scheduler also retries failed jobs, multiplying this.

The README claims "**Stateless & Idempotent**" (`README.md:18`). The playlist write is idempotent; the notification is not.

**Fix (minimal):** only notify when the track was newly filed.

```python
if playlist_added:
    telegram_sent = telegram.send_track_notification(...)
```

This makes the playlist the dedupe ledger, which is consistent with the stateless design. **Fix (correct):** keep a small persisted set of notified track IDs — see [§6 of the addendum](#6-the-state-layer-concretely).

### H-3. Retry policy silently excludes every POST
`soundcloud_client.py:34-39`, `telegram_notifier.py:16-21`

`urllib3.util.Retry` defaults `allowed_methods` to idempotent verbs only. Verified on the installed urllib3 2.6.3:

```
allowed_methods: frozenset({'OPTIONS','HEAD','TRACE','PUT','DELETE','GET'})
POST in allowed: False
```

So the retry configuration — including `status_forcelist=[429, ...]` — **does not apply to** OAuth token refresh (`POST /oauth2/token`), playlist creation (`POST /playlists`), the follow fallback (`POST /users/{id}/follow`), or **Telegram `sendMessage`**, which is the single call most likely to be rate-limited (Telegram enforces ~30 msg/s and 429s aggressively). The code reads as if these are protected. They are not.

**Fix:** add `allowed_methods=frozenset({"GET","PUT","POST","DELETE","HEAD","OPTIONS","TRACE"})` to both `Retry` objects. POST is safe to retry here: token refresh is idempotent-enough, `sendMessage` at worst duplicates a message, and playlist creation is guarded by the title lookup. Note that on urllib3 1.26 (permitted by `requirements.txt`) the kwarg exists but `method_whitelist` is the deprecated alias — pin `urllib3>=2.0` to avoid ambiguity.

### H-4. Terraform clobbers the rotated refresh token on every apply
`terraform/main.tf:50-54`

```hcl
resource "google_secret_manager_secret_version" "versions" {
  for_each    = local.secrets_map
  secret_data = each.value          # value from terraform.tfvars
}
```

Combined with `version = "latest"` on the env mounts, any `terraform apply` after a rotation re-writes the **original** `tfvars` refresh token as the newest version, invalidating the live one. Config and rotating runtime state are being managed by the same resource.

**Fix:** manage the *secret container* in Terraform but not the rotating *version*. Seed `soundcloud-refresh-token` once out-of-band (or via a `lifecycle { ignore_changes = [secret_data] }` + `create_before_destroy`), and exclude it from `local.secrets_map`'s version loop.

### H-5. No pagination on playlists → duplicate playlists created
`soundcloud_client.py:171`

`GET /me/playlists` returns at most 50 by default with no `linked_partitioning`. A user with more than 50 playlists will not have `Genre: Techno` in the response, so `target_playlist` is `None` and the code takes the **create** branch (line 246) — producing a duplicate playlist with the same title, every run, forever.

**Fix:** paginate with `?limit=200&linked_partitioning=true` and follow `next_href` until exhausted; or query by title if the API supports it.

### H-6. No pagination on likes → likes silently dropped
`soundcloud_client.py:107`

`?limit=50` hardcoded, no pagination. Any run where more than 50 relevant likes exist loses the remainder with no warning. Once C-1 is fixed and the filter actually matches tracks, this becomes the next visible failure — a first run against an established account would need to page through thousands of likes.

**Fix:** paginate, and cap total work per invocation with an explicit logged limit (see H-7).

### H-7. Sequential processing will exceed the scheduler deadline
`main.py:82-127`, `terraform/main.tf:161`

Each track costs 3–4 **sequential** HTTPS round trips (follow PUT, optional playlist detail GET, playlist PUT/POST, Telegram POST). At 50 tracks and a conservative 300 ms per call that is ~60 s minimum; with `backoff_factor=1` retries on any 429/5xx it climbs fast. `attempt_deadline = "180s"` on the scheduler job, against a Cloud Run default request timeout of 300 s, means the **scheduler gives up while the service is still working** — then retries, re-notifying everything already processed (compounding H-2).

**Fix:**
- Raise `attempt_deadline` toward the Cloud Run timeout (`"600s"` with an explicit `timeout = "600s"` on the service), **and**
- process tracks concurrently with a bounded `ThreadPoolExecutor(max_workers=4)` — `requests.Session` is not thread-safe for mutation, so give each worker its own session or serialize the token refresh behind a lock, **and**
- cap tracks per invocation and `log()` when the cap truncates work.

### H-8. Failed playlist fetch is cached as "no playlists" behaviour
`soundcloud_client.py:174-175`

```python
if not response.ok:
    return []
```

Two problems. First, the empty list is **not** stored in `_playlists_cache`, so every track in the run re-issues the failing request — verified: 4 calls for 4 invocations. Second and worse, returning `[]` is indistinguishable from "user has no playlists," so `add_track_to_genre_playlist` proceeds to the **create** branch. A transient 503 on the playlist list therefore causes duplicate playlist creation rather than a clean failure.

**Fix:** `raise RuntimeError` on a non-OK response. The caller in `main.py:99-102` already catches per-track exceptions and logs them.

---

## Medium (P2)

### M-1. Camelot key regex produces false positives
`soundcloud_client.py:288-292`

The third pattern, `r'\b([1-9]|1[0-2])([AB])\b'`, matches any digit-letter pair anywhere in the concatenated tags+title+description. Verified false positives:

| Input | Detected key |
|---|---|
| `"Vol 2B Continued"` | `2B` |
| `"Live at Studio 8a"` | `8A` |
| `"Set 2 (Part 12b)"` | `12B` |
| `"Chapter 3 Ambient"` / desc `"released 1b"` | `1B` |

The commit message for `4edef2e` and the inline comment at line 287 claim this was hardened ("prevent false positives like 'Part 1B'") — the first two patterns were added, but the permissive third pattern is still reached whenever they miss, so nothing was actually prevented.

**Fix:** restrict the bare pattern to the `tag_list` field only (where `8A` is conventionally a real key tag) and require the prefixed/bracketed forms in title and description. Or drop pattern 3 and accept lower recall — a wrong key is worse than "Not specified" for a DJ tool.

### M-2. `C Major` is truncated to `C`
`soundcloud_client.py:302`

`r'\bkey[:\s\-]+([A-G][#b]?(?:m|min|maj|minor|major)?)\b'` has no space between the note and the quality, so `"Key: C Major"` captures only `C`. Verified: `'Key: C Major'` → `'C'`. `README.md:16` explicitly advertises `C Major` support, and `test_app.py:36-44` only covers the `key_signature` API-property path, so this is untested.

**Fix:** `([A-G][#b]?)\s*(m|min|maj|minor|major)?\b` and normalize the two groups. Also review `.capitalize()` at line 308 — it lowercases everything after the first character, which is right for `F#m` but destroys `Bb` → `Bb` (ok) and any multi-word result.

### M-3. Unhandled `TypeError` on malformed `lookback_minutes`
`main.py:43-48`

```python
try:
    parsed_val = int(custom_lookback)
    ...
except ValueError:
    pass
```

`custom_lookback` may come from `request_obj.get_json()` (line 41), so it can be a list or dict. `int([])` raises **`TypeError`**, not `ValueError` — verified — and this block sits *before* the outer `try` at line 77. The exception escapes to functions-framework, returning a 500 with a stack trace rather than the JSON error contract the rest of the handler maintains.

**Fix:** `except (ValueError, TypeError): pass`.

### M-4. Malformed env var crash-loops the container
`config.py:22`

`LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "65"))` executes at **class-definition time**, i.e. at import. A non-numeric value raises `ValueError` during module load, the container fails to start, and Cloud Run reports an opaque startup-probe failure rather than a config error. `variables.tf:51` types `lookback_minutes` as `string` with no validation, so a typo in `tfvars` reaches the container unchecked.

**Fix:** parse defensively with a fallback and a warning log, and add a Terraform `validation` block asserting the value is numeric.

### M-5. Internal error detail returned to the HTTP caller
`main.py:136-141`, `soundcloud_client.py:62,115,233,261`

```python
except Exception as err:
    return jsonify({"status": "error", "message": str(err)}), 500
```

Those exception strings embed upstream `response.text` verbatim, including SoundCloud OAuth error bodies. The endpoint is IAM-protected (see the Positives section), so the blast radius is limited to principals holding `run.invoker` — but returning raw upstream bodies is a habit worth breaking, and it makes the response contract unstable.

**Fix:** log the detail server-side with `logging.exception`, return a generic message plus a correlation ID.

### M-6. Timestamp handling is inconsistent and lossy
`soundcloud_client.py:129-144`

- A **parse failure** hits `continue` (line 144) — the track is dropped.
- A **missing** timestamp falls through to `recent_tracks.append(track)` — the track is kept.

These are opposite defaults for the same condition (unknown recency), and neither is justified. The warning text ("Skipping track timestamp check") also contradicts the code, which skips the *track*, not the check.

The normalization at line 132 is narrowly targeted: `.replace(" +0000", "+00:00")` handles only UTC offsets. Non-UTC offsets survive on Python 3.14 but this repo's container is **Python 3.11** (`Dockerfile:1`) while local tests run 3.14 — `fromisoformat` leniency differs materially between those versions, so local green tests do not prove container behaviour.

**Fix:** decide one policy (recommend: treat unknown recency as *not recent* and log at warning), fix the message, and pin local development to 3.11 to match the container.

### M-7. Fallback endpoints appear to be fabricated
`soundcloud_client.py:112, 162-164, 258`

Three fallbacks target endpoints that are not part of the current public SoundCloud API: `GET /me/favorites` (removed), `POST /users/{id}/follow`, and `POST /me/playlists`. Each adds a guaranteed-failing round trip on the error path and, worse, converts a clear "endpoint returned 403" into a confusing double failure. `follow_artist` in particular will always burn two requests when the primary `PUT` fails.

**Fix:** remove the fallbacks, or verify them against the API docs and comment why each exists. Let real errors surface.

### M-8. `follow_artist` conflates "error" with "already following"
`soundcloud_client.py:150-164`, `telegram_notifier.py:52`

`follow_artist` returns `False` on any non-2xx. The notifier renders that as `"⚠️ Already Following / Skipped"`. So a 403 (insufficient scope), a 429, or a network failure all report to the user as a benign "already following." Real follow failures are invisible.

**Fix:** return a tri-state (`FOLLOWED` / `ALREADY_FOLLOWING` / `FAILED`) by distinguishing status codes, and render each distinctly.

### M-9. Cloud Run runs with an unspecified, over-privileged identity
`terraform/main.tf:62-63`

No `service_account` on the `template` block ⇒ default compute SA, which on older projects carries `roles/editor` — full read/write over the entire project, for a service that needs to read five secrets and make outbound HTTPS calls. Fixed by the C-2 patch; called out separately because it is a distinct least-privilege violation.

### M-10. Playlists are created public by default
`soundcloud_client.py:250`

```python
"sharing": "public",
```

Every auto-created genre playlist is world-visible on the user's profile, exposing their listening habits without an opt-in. Neither the README nor `.env.example` mentions this.

**Fix:** default to `"private"` and add a `PLAYLIST_SHARING` config knob.

### M-11. Container runs as root
`Dockerfile:1-20`

No `USER` directive. The process runs as UID 0 inside the container. Cloud Run's gVisor sandbox mitigates the impact, but this is a one-line hardening fix and a standard audit finding.

**Fix:**
```dockerfile
RUN useradd --create-home --uid 10001 appuser
USER 10001
```

### M-12. Unpinned dependencies
`requirements.txt`

All six entries use `>=` with no upper bound, no lockfile, and no hashes. Two consequences: builds are not reproducible (a rebuild months apart yields different trees), and a compromised or breaking release of any transitive dependency lands in production automatically. `urllib3>=1.26.0` in particular spans the 1.x→2.x boundary, which changed `Retry` semantics relevant to H-3.

**Fix:** pin exact versions, generate `requirements.lock` with `pip-compile --generate-hashes`, and install with `--require-hashes`. Add Dependabot or Renovate for controlled bumps.

### M-13. No remote Terraform backend → secrets in plaintext on disk
`terraform/main.tf:1-9`

No `backend` block, so state is local `terraform.tfstate`. Terraform state stores `secret_data` **in plaintext** regardless of the `sensitive = true` markers in `variables.tf` (those only suppress CLI output). All five credentials therefore sit unencrypted in the working directory. `.gitignore:22-23` correctly excludes it from git, which is good — but that is the only control.

**Fix:** configure a GCS backend with CMEK and versioning:
```hcl
backend "gcs" { bucket = "tfstate-<project>" prefix = "soundcloud-bot" }
```

### M-14. `print()` instead of structured logging
throughout `main.py`, `soundcloud_client.py`, `telegram_notifier.py`

Everything goes to stdout with no severity. In Cloud Logging every line is `INFO`, so errors cannot be alerted on, log-based metrics cannot distinguish failure from success, and there is no request correlation across the per-track loop.

**Fix:** `google-cloud-logging`'s standard-library handler, or emit JSON with a `severity` field. Warnings at `soundcloud_client.py:75,143` and errors at `main.py:94,102,117,137` should be `WARNING`/`ERROR`.

### M-15. Test coverage gaps
`test_app.py`

8 passing tests, all narrowly scoped to key extraction, one date-filtering case, and two playlist paths. **Not covered:** `main.py` end-to-end (the request handler, the lookback clamp, the credential guard, the per-track error isolation), `ensure_access_token`, the 401 refresh-and-retry in `_make_request`, `follow_artist`, `_persist_refreshed_token`, every error/fallback branch, and playlist creation. `functions_framework` is **not installable in the local environment** (verified `ModuleNotFoundError`), which is likely why `main.py` has zero tests — the handler can and should be tested with a plain Flask test client instead.

Also, as noted in C-1, `test_app.py:55-60` asserts against a response shape the real API does not produce, making the suite worse than no test for that path.

**Fix:** add `pytest`, `pytest-cov`, and `responses` (or `requests-mock`) to a `requirements-dev.txt`; test `main` via `Flask.test_client()`; add cases for each fallback and error branch; set a coverage floor in CI.

---

## Low (P3)

| # | Finding | Location |
|---|---|---|
| L-1 | `request` imported from flask but never used (the handler uses `request_obj`) | `main.py:3` |
| L-2 | `session.mount("http://", adapter)` is dead config — `BASE_URL` is HTTPS-only; remove so a future plaintext URL fails loudly | `soundcloud_client.py:42` |
| L-3 | No scheme allowlist before embedding `permalink_url` in a Telegram `href`. Escaping is correct, and Telegram restricts schemes, so this is defence-in-depth only | `telegram_notifier.py:40-46` |
| L-4 | `expires_in` from the token response is ignored; the client relies entirely on reactive 401 handling, costing one wasted request per expiry | `soundcloud_client.py:64-65` |
| L-5 | Python version drift: container is 3.11, local test runs 3.14. `datetime.fromisoformat` leniency differs between them (see M-6) | `Dockerfile:1` |
| L-6 | `COPY . .` before the CMD invalidates the layer cache on any source change; requirements are correctly copied first, so only the source layer rebuilds — minor. No `HEALTHCHECK` | `Dockerfile:14` |
| L-7 | No `max_instance_count` or `max_instance_request_concurrency` on the Cloud Run service. For a cron-driven job, cap at 1–2 instances to prevent overlapping runs from racing on playlist writes (see C-5) | `terraform/main.tf:62-71` |
| L-8 | `ingress = "INGRESS_TRAFFIC_ALL"` is acceptable because IAM auth is required, but there is no app-layer authorization as defence-in-depth — a future accidental `allUsers` binding would fully expose the endpoint | `terraform/main.tf:60` |

---

## What the code gets right

Worth stating explicitly, because several of these are commonly missed:

- **The endpoint is genuinely IAM-protected.** No `allUsers` binding exists; only the dedicated invoker SA holds `roles/run.invoker` (`main.tf:148-153`), and the scheduler authenticates with an OIDC token (`main.tf:167-169`). This is correct and is the reason several information-disclosure findings above are Medium rather than High.
- **Secrets never touch source or environment files in git.** `.gitignore` covers `.env` and `*.tfvars` with a correct `!terraform.tfvars.example` negation; `.dockerignore` excludes `.env*`, `terraform/`, and `.git`. `.env.example` contains only placeholders.
- **Every outbound request has an explicit timeout** (15 s SoundCloud, 10 s Telegram). This is the single most commonly omitted reliability control in code like this.
- **HTML escaping in the notifier is done properly** — `html.escape` on every interpolated field, `quote=True` on values landing inside attribute quotes, and a test (`test_app.py:116-135`) that actually asserts on `&amp;` and `&lt;`. No injection into the Telegram message.
- **Datetimes are timezone-aware** (`datetime.timezone.utc`), avoiding the naive/aware comparison bug that `4edef2e` fixed.
- **Connection pooling via `requests.Session`** on both clients, so the per-track loop reuses TLS connections rather than renegotiating.
- **Per-track error isolation** in `main.py:82-127`: one bad track does not abort the run, and the response reports per-track outcomes.
- **`raise_on_status=False`** on the `Retry` objects, letting the code inspect status codes rather than catching adapter exceptions.
- **Both int-ID and dict-shaped track arrays are handled** (`soundcloud_client.py:214-220`), with a test — real defensive coding against an inconsistent API.
- **Terraform is clean and idiomatic**: `for_each` over a secrets map, `sensitive = true` on credential variables, `disable_on_destroy = false` on API enablement, explicit `depends_on` chains, and useful outputs.

---

## Prioritized remediation plan

**Before this can run at all**
0. **Add the state layer first** — one GCS object + Secret Manager for the rotating token (addendum §6). It is a prerequisite for a correct C-1 and C-4 fix, so doing it first avoids implementing those twice.
1. C-1 — fix the like-recency logic and replace the misleading test fixture
2. C-2 — add runtime SA + `secretmanager.secretAccessor`
3. C-3 — add the image build, or correct the README's automation claims
4. C-4 — fix project-ID resolution, grant `secretVersionAdder`, log skipped persistence
5. C-5 — fetch authoritative playlist state before every write

**Before this can run unattended**
6. H-1 — fail hard on a create response with no ID
7. H-2 — gate notification on `playlist_added`
8. H-3 — add `allowed_methods` to both `Retry` objects; pin `urllib3>=2`
9. H-4 — stop managing the rotating refresh-token version in Terraform
10. H-5/H-6 — paginate playlists and likes
11. H-7 — raise the scheduler deadline and parallelize the track loop
12. H-8 — raise on playlist-fetch failure instead of returning `[]`

**Hardening**
13. M-11/M-12/M-13 — non-root container, pinned+hashed deps, GCS backend with CMEK
14. M-9/M-10 — least-privilege runtime SA, private playlists by default
15. M-14 — structured logging with real severities
16. M-1/M-2 — tighten the key regexes; add the false-positive cases as tests
17. M-3/M-4/M-5/M-6 — input validation, safe config parsing, generic error responses, one consistent timestamp policy
18. M-15 — `requirements-dev.txt`, `main.py` handler tests, coverage floor in CI

**Cleanup**
19. L-1 … L-8

---

## Suggested next steps

There is no CI in this repository. Adding a GitHub Actions workflow that runs `python -m unittest`, `ruff check`, `pip-audit`, `terraform validate`, and `tfsec`/`checkov` would have caught the unused import, the unpinned dependencies, the missing IAM binding, and the local-state finding automatically. That is probably the highest-leverage single addition after the P0 fixes.

---
---

# Addendum — Would a different language make this app better?

**Short answer: no. Keep Python.** But the question points at a real improvement, and it is one layer up: **this should not be an HTTP service at all.** Details below.

## 1. The decisive test: would a rewrite have prevented any of the defects?

A language change is worth its cost only if it eliminates a class of bug this app actually has. Scoring every P0/P1 finding:

| # | Defect | Prevented by static typing / another language? |
|---|---|---|
| C-1 | Filters on track upload time, not like time | ❌ No — domain knowledge about what the endpoint returns. A typed `Track{CreatedAt}` gets populated from the same wrong field. |
| C-2 | Missing `secretAccessor` IAM binding | ❌ No — Terraform. |
| C-3 | No container image build | ❌ No — Terraform / CI. |
| C-4 | `GCP_PROJECT` is not set on Cloud Run | ❌ No — every language reads env vars by string key. |
| C-5 | Playlist `PUT` replaces instead of appends | ❌ No — API semantics. |
| H-1 | `PUT /playlists/None` | 🟡 Partial — a non-nullable `PlaylistID` type would make the placeholder unrepresentable. This is the one finding a type system genuinely kills. |
| H-2 | Duplicate Telegram notifications | ❌ No — missing state, not a type error. |
| H-3 | `Retry` excludes POST | 🟡 Partial — Go's stdlib has no retry, so you would write it explicitly and likely get it right. But every ecosystem's retry library has its own defaults to misread. |
| H-4 | Terraform clobbers rotated token | ❌ No — Terraform. |
| H-5/H-6 | No pagination | ❌ No. |
| H-7 | Sequential loop vs 180 s deadline | 🟡 Partial — `errgroup` is nicer than `ThreadPoolExecutor`, but the Python version is ~10 lines. |
| H-8 | Failed fetch returns `[]` | ❌ No — `Result`/`error` return conventions help culturally, but Python can `raise` just as well. |

**Score: 0 of 5 criticals, ~1 of 8 highs.** Every blocker is a domain-semantics bug, an infrastructure gap, or a missing-state bug. None is a Python bug. A rewrite would faithfully port all five criticals into a new language — with fewer tests than the current 8.

## 2. Performance and cost: measured, not assumed

Cloud Run request-based billing against the current `main.tf` config (1 vCPU, 512 MiB) and an hourly cron (~730 invocations/month):

```
  10s/run ->     7,300 vCPU-s/mo (free tier 180,000) | billed $0.0003/mo
  30s/run ->    21,900 vCPU-s/mo (free tier 180,000) | billed $0.0003/mo
  60s/run ->    43,800 vCPU-s/mo (free tier 180,000) | billed $0.0003/mo
 180s/run ->   131,400 vCPU-s/mo (free tier 180,000) | billed $0.0003/mo
```

**Even at the 180-second scheduler deadline, the workload sits entirely inside the Cloud Run free tier.** The `$0.0003` is the per-request charge. A language that runs 10× faster saves **$0.00**.

That is the whole performance argument, because this workload is:
- **IO-bound**, not CPU-bound — the wall clock is SoundCloud and Telegram round trips. Interpreter speed is invisible; a Go rewrite would wait on exactly the same network.
- **Concurrency 1** — one invocation per hour. No throughput ceiling to raise.
- **CPU-trivial** — a few regex matches over a few short strings.

The only real CPU work in the whole app is `extract_musical_key`, and it runs on maybe 50 short strings per hour.

**Cold start** is the one place a compiled language wins. Measured locally (Windows, warm `.pyc`):

```
import requests, grpc                               1895 ms
import config, soundcloud_client, telegram_notifier  114 ms
```

Container Linux numbers would be lower, and these are not a reliable proxy — but the shape holds: Python's import cost is order ~0.5–1 s, and Go's is ~0. That saving arrives **once per hour, against a run that takes 30+ seconds**, on a schedule where nothing is waiting for a response. It is worth nothing here. (Note the `grpc` delta: that is what `google-cloud-secret-manager` drags in, and the lazy import at `main.py:19` already correctly defers it off the hot path — good instinct by whoever wrote that.)

## 3. What a rewrite would actually cost

The app is **563 SLOC including tests** — which makes a rewrite look cheap. It isn't, because the lines are not the asset. The asset is the accumulated knowledge of SoundCloud's undocumented behaviour, currently encoded as:

- track arrays arrive as either `[123, 456]` or `[{"id": 123}]` (`soundcloud_client.py:214-220`)
- timestamps arrive as `2024/01/15 10:30:00 +0000`, not ISO 8601 (`soundcloud_client.py:132`)
- playlist `tracks` arrays come back truncated from list endpoints (C-5)
- `PUT /playlists/{id}` is replace-not-append (C-5)
- the OAuth token endpoint rotates refresh tokens (C-4)

C-1 and C-5 prove this domain model is **still being learned** — the code currently gets both wrong. Rewriting now means re-deriving all of it against a live third-party API, in a language with zero existing tests, while the semantics are still unsettled. That is the worst possible moment to change runtime.

Get the domain model correct in Python, with tests pinning each quirk. *Then*, if you still want Go, you have a specification to port against.

## 4. Honest case for the alternatives

**Go** — the only credible contender. Genuine wins:
- Static binary → `scratch`/`distroless` image at roughly 15–25 MB versus ~250 MB for `python:3.11-slim` plus deps. Faster deploys, and the interpreter, `pip`, and shell all leave the attack surface. This partly subsumes M-11 (root user) and M-12 (unpinned deps) — a `scratch` image has no shell to exploit and `go.sum` pins by hash by default.
- `errgroup.SetLimit(4)` for H-7 is cleaner than juggling thread-unsafe `requests.Session` objects.
- Compile-time nil-safety kills H-1.

Real but modest — call it a 10–15% improvement in ops posture, for a full rewrite and the loss of 8 tests. **Not worth it now.** Worth reconsidering *after* the P0/P1 fixes land, if you ever have a reason to touch it wholesale.

**TypeScript / Node** — no compelling edge over Python here. Similar image size, similar cold start, similar ecosystem. Switching buys nothing.

**Rust** — a clear over-fit. Cold-start and image-size benefits comparable to Go, at substantially higher development cost, for a service whose bottleneck is a third party's HTTP latency.

**Python's remaining edge:** if the README roadmap (`README.md:119`) genuinely heads toward LLM-based genre/key enrichment, Python has the deepest ecosystem for it. (Separately — `"Gemini 3.5 Flash"` in that line is not a real model name; worth correcting in the docs.)

## 5. The change that *would* make this meaningfully better — drop the HTTP layer

This is the answer worth acting on, and it is language-agnostic.

**The app is an hourly batch job wearing a web server.** It serves one request per hour, yet carries `functions-framework` + `Flask` + `gunicorn`, and the Terraform carries an ingress configuration, an invoker service account, OIDC token minting, and a request-timeout ceiling. All of that is accidental complexity from the HTTP shape. **Cloud Run Jobs** is the right primitive:

| | Cloud Run **Service** (today) | Cloud Run **Job** |
|---|---|---|
| Deps | `functions-framework`, `Flask`, `gunicorn` | none — a plain `python main.py` |
| Listening socket | yes — public ingress + IAM | **none — nothing to attack** |
| Max runtime | request timeout (300 s default) | up to 24 h per task |
| H-7 deadline mismatch | structural problem | **dissolves** |
| Retries | hand-rolled; scheduler retries duplicate work | built-in task retry with backoff |
| Terraform surface | ingress, invoker SA, OIDC, `run.invoker` | scheduler → `jobs:run`, that's it |

Concretely, this **deletes** L-8 (ingress exposure), the entire `INGRESS_TRAFFIC_ALL` question, M-5's severity (no HTTP response body to leak into), and H-7's root cause. It removes **3 of 6 dependencies** — shrinking both the image and the supply-chain surface addressed in M-12. It keeps every line of business logic.

```hcl
resource "google_cloud_run_v2_job" "sync" {
  name     = var.service_name
  location = var.region
  template {
    task_count = 1
    template {
      service_account = google_service_account.runtime_sa.email
      max_retries     = 3
      timeout         = "1800s"
      containers { image = "..." }   # same container, CMD ["python", "main.py"]
    }
  }
}
# Scheduler then POSTs to:
#   https://run.googleapis.com/v2/projects/${p}/locations/${r}/jobs/${name}:run
# with an oauth_token, scope https://www.googleapis.com/auth/cloud-platform
```

`main.py` keeps its logic and loses the `functions_framework` decorator and the `__main__` Flask block. As a bonus, that also fixes M-15's testability complaint — `functions_framework` is not even installable in the current dev environment, and with a Job it is no longer needed.

**And the deeper architectural point:** C-1, C-4, and H-2 all reduce to the same root cause — *the app has no persistent state.* The README markets "Stateless & Idempotent" (`README.md:18`) as a feature, but statelessness is precisely what makes the recency filter unfixable, the token rotation unstorable, and the notifications un-dedupable. Giving the app somewhere to remember about three small facts fixes three findings at once and is worth far more than any language swap — see the next section.

## 6. The state layer, concretely

### Why any state at all

The app runs for ~30 seconds every hour and then dies. Nothing survives. So each run starts with total amnesia and must answer one question: **"what is new since last time?"**

With no memory it can only guess, and it guesses with a clock — *"give me things from the last 65 minutes."* Three of the defects above are downstream of that single guess:

| Defect | What the guess costs |
|---|---|
| **C-1** | It reads the wrong clock — track *upload* time, not *like* time. Like a song from 2019 and the app concludes "uploaded 7 years ago, not recent, skip." It does nothing. |
| **H-2** | A time window is inherently fuzzy. A 65-minute window on a 60-minute schedule overlaps by 5 minutes *on purpose* (so nothing falls through a gap), which means some tracks land in two runs → duplicate Telegram messages. |
| **C-4** | SoundCloud returns a fresh refresh token and says "use this next time." There is no next time and nowhere to write it, so it is discarded and the following run authenticates with a dead token. The service stops permanently. |

If the app could instead remember **"last run I stopped at like #12345,"** C-1 and H-2 simply do not arise. No clock, no window, no overlap — it resumes exactly where it left off, and `LOOKBACK_MINUTES` can be deleted along with the whole timestamp-parsing block (`soundcloud_client.py:129-144`, which also retires M-6).

What needs to persist is small:

1. the last like processed (a track ID or pagination cursor)
2. the current refresh token
3. *optionally* the set of track IDs already notified about

That is a sticky note, not a database.

### Recommended: one GCS object, plus Secret Manager for the token

| Option | Fit |
|---|---|
| **A single JSON object in a GCS bucket** | ✅ **Recommended.** ~15 lines: read blob, use it, write it back. Well inside the always-free tier at 730 reads + 730 writes per month. Nothing to provision beyond one bucket. |
| **Secret Manager** for the refresh token only | ✅ A rotating refresh token genuinely *is* a secret, and Secret Manager is already in the stack. This is what `main.py:8-28` was already trying to do — it is only broken, not misconceived. |
| **Firestore** | 🟡 Not wrong, but more machinery than one blob needs. It earns its place only if the notified-ID set grows large enough to want queries, or if you later run overlapping invocations and need atomic document updates. |
| **In-container disk / `/tmp`** | ❌ Does not survive between invocations. Not an option. |

So: **GCS for the position marker, Secret Manager for the token.** Note the split is deliberate — keep secrets in the secret store and non-sensitive bookkeeping in object storage, rather than versioning a cursor into Secret Manager (which is rate-limited and would accumulate versions forever).

### Terraform

```hcl
resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-${var.service_name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }   # cheap insurance against a corrupt write

  lifecycle_rule {
    condition { num_newer_versions = 10 }
    action     { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "state_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"   # object-scoped read+write, not bucket admin
  member = "serviceAccount:${google_service_account.runtime_sa.email}"
}
```

Then add `STATE_BUCKET` as a plain `env` block alongside `LOOKBACK_MINUTES`, and grant the runtime SA `roles/secretmanager.secretVersionAdder` on `soundcloud-refresh-token` only (per C-4).

### Application shape

```python
import json
from google.cloud import storage

_STATE_OBJECT = "state.json"

def load_state(bucket_name: str) -> dict:
    """Returns {} on first run or if the object is missing/corrupt."""
    blob = storage.Client().bucket(bucket_name).blob(_STATE_OBJECT)
    try:
        return json.loads(blob.download_as_bytes())
    except Exception as e:          # NotFound, JSONDecodeError
        logging.warning("No usable state object, treating as first run: %s", e)
        return {}

def save_state(bucket_name: str, state: dict) -> None:
    blob = storage.Client().bucket(bucket_name).blob(_STATE_OBJECT)
    blob.upload_from_string(json.dumps(state), content_type="application/json")
```

State document:

```json
{
  "last_processed_like_id": 12345678,
  "notified_track_ids": [12345678, 12345677, "... capped at ~500, FIFO"],
  "updated_at": "2026-08-04T18:00:00+00:00"
}
```

Three implementation notes that matter:

- **Write the marker only after a track fully succeeds.** If the run dies halfway, the next run resumes from the last *confirmed* track rather than skipping the remainder. That converts H-7's timeout from data loss into a harmless partial run.
- **Cap `notified_track_ids`** (FIFO, ~500 entries). Unbounded growth is how a "sticky note" quietly becomes a 10 MB blob you re-download every hour.
- **A corrupt or missing object must mean "first run," not a crash.** The `try/except` above is load-bearing — and unlike the current silent failures (C-4), it logs.

Cost: 730 reads + 730 writes per month against a free tier of 50,000 Class-B and 5,000 Class-A operations. **Free**, like the compute.

### Why this is worth more than a rewrite

This is one bucket, ~15 lines of Python, and one `env` var. It root-causes **C-1, C-4, and H-2**, lets you delete the entire timestamp-parsing block (retiring **M-6**), removes the need for `LOOKBACK_MINUTES`, and makes H-7's deadline overrun non-destructive. Compare that to a full-language rewrite, which by the accounting in §1 prevents **zero** criticals.

One honest caveat: this makes the app stateful, which contradicts a marketed feature (`README.md:18`). Update the README — statelessness is not a feature here, it is the root cause of three critical bugs.

## 7. Bottom line

| Option | Verdict |
|---|---|
| Rewrite in Go / Rust / TS | ❌ **No.** Prevents 0 of 5 criticals. Saves $0. Discards 8 tests and hard-won API knowledge while the domain model is still wrong. |
| Stay on Python | ✅ **Yes.** Fix the 5 criticals and 8 highs; the runtime was never the problem. |
| Cloud Run **Job** instead of Service | ✅ **Yes — do this.** Removes 3 deps and the whole ingress attack surface, and structurally fixes H-7. Language-agnostic. |
| Add persistent state (**one GCS object** + Secret Manager for the token) | ✅ **Yes — do this.** ~15 lines and one bucket. Root-causes C-1, C-4, and H-2 together, and retires M-6. Firestore is unnecessary here — see §6. |
| Revisit Go later | 🟡 Defensible *after* P0/P1 land, purely for the distroless-image win. Not now. |

The honest summary: **you cannot fix this app by changing its language, and you would not measurably improve it either.** Every blocker lives in the domain logic and the Terraform. Spend the effort there and on the platform shape, not on the runtime.

---
---

# Verification Pass — commit `c7ee3c6`

**Date:** 2026-08-04
**Reviewed:** `c7ee3c6 fix: resolve all code review findings (P0, P1, P2, P3 & GCS state layer)`
**Test status:** `python -m unittest test_app` → **8 passed, 0 failed**

**Result: 26 of 36 findings fully fixed, 5 partial, 5 not done — and 3 new defects introduced.** The most serious is **V-1: the new state marker never advances past the first run**, which defeats the C-1 fix it was built to enable.

| | Count | |
|---|---|---|
| ✅ Fully fixed & verified | 26 | C-2, C-3, C-4, C-5, H-1, H-2, H-3, H-5, H-8, M-1…M-11, L-1, L-2, L-3, L-7 |
| 🟡 Partial | 5 | C-1, H-4, H-7, M-12, M-14 |
| ❌ Not done | 5 | H-6, M-13, M-15, L-4, L-5/L-6 |
| 🔴 **New defects** | 3 | V-1, V-2, V-3 |

---

## 🔴 New defects introduced

### V-1 (critical). `last_processed_like_id` never advances past the first run
`main.py:119`, `main.py:136-137`, `main.py:191`

```python
new_highest_like_id = last_processed_like_id        # line 119 — non-None from run 2 onward
...
    if new_highest_like_id is None:                 # line 136 — therefore never true again
        new_highest_like_id = track_id
```

The guard only fires when the marker is `None`, which is true **only on the very first run**. Simulated over three runs:

```
run 1: processed [100, 99]       -> saved marker = 100
run 2: processed [105, 104, 103] -> saved marker = 100
run 3: processed [110, 109]      -> saved marker = 100
                                    EXPECTED 110
```

**Consequence:** the resume boundary is frozen at run 1's newest track forever. `get_recent_likes` stops scanning at `track_id == last_processed_like_id` (`soundcloud_client.py:150`), so **every run re-scans the entire like history back to run 1**, growing without bound. The state layer's whole purpose — resume exactly where you left off — does not work, which means **C-1 is not actually fixed end to end**.

Re-processing is currently masked by the `processed_ids` skip at `soundcloud_client.py:154`, so wasted API calls are bounded by the notified set staying correct — but V-2 breaks exactly that, and the two together resurrect H-2. Secondary fragility: if the sentinel track is ever *unliked*, the stop condition can never match and every run scans the full 50-item page.

**Fix** — advance the marker to the newest track, after the loop completes so a mid-run failure resumes rather than skips (likes arrive newest-first):

```python
processed_ok = []
for track in recent_tracks:
    ...
    processed_ok.append(track_id)          # append after the track fully succeeds

if processed_ok:
    new_highest_like_id = processed_ok[0]  # newest; everything below it is also done
```

Delete the `if new_highest_like_id is None` guard at lines 136-137.

### V-2 (high). The `notified_track_ids` cap is not FIFO — it drops recent IDs
`main.py:116`, `main.py:192`, `state_manager.py:44-45`

`state_manager.save_state` slices `[-500:]` and documents it as "FIFO," but the data reaches it through a **`set`**: loaded as `set(...)` at `main.py:116`, dumped as `list(notified_track_ids)` at `main.py:192`. Python sets are not insertion-ordered, so `[-500:]` keeps an arbitrary 500, not the newest 500. Measured with realistic 9-digit track IDs, 600 accumulated:

```
of the 50 MOST RECENT ids, dropped by the cap: 6
```

**Consequence:** once a user passes ~500 processed tracks, recently-notified IDs start falling out of the ledger. Combined with V-1 (which re-scans them every run), those tracks get **re-notified — the duplicate Telegram messages H-2 was fixed to prevent**.

**Fix:** keep insertion order; use a list for the ledger and a set only for lookup.

```python
notified_track_ids = list(app_state.get("notified_track_ids", []))   # ordered ledger
notified_lookup    = set(notified_track_ids)                         # O(1) membership
...
if telegram_sent:
    notified_track_ids.append(track_id)
    notified_lookup.add(track_id)
...
app_state["notified_track_ids"] = notified_track_ids   # now [-500:] is genuinely FIFO
```

Pass `notified_lookup` (not the list) as `processed_ids` to `get_recent_likes`.

### V-3 (medium). With no `STATE_BUCKET`, there is now **no** recency filter at all
`soundcloud_client.py:157-159`, `config.py:41`

The C-1 fix correctly stopped treating a bare track's `created_at` as a like time — the timestamp branch now runs only when `is_wrapped` is true. But the real API returns bare tracks, so in practice that branch never executes, and with no state passed **nothing filters**: verified that two tracks uploaded in 2019/2020 both return under `lookback_minutes=65`.

`STATE_BUCKET` is therefore now *mandatory* for correct behaviour, yet `config.py:41` defaults it to `""` and nothing warns. Terraform does wire it (`main.tf:174-177`), so deployed runs are fine — but any local run, or a deploy that omits the env var, silently reprocesses the newest 50 likes every hour. `lookback_minutes` is dead config on this path while still being validated and plumbed through three files.

**Fix:** log a prominent warning at startup when `STATE_BUCKET` is empty, and either honour `lookback_minutes` as a fallback bound or document it as wrapper-only.

### V-4 (nit). Dead assignment
`soundcloud_client.py:283` — `updated_playlist = res.json() if res.text else full_playlist_data` is assigned and never read.

---

## 🟡 Partial

- **C-1** — the wrong-clock bug is genuinely gone, but the replacement resume mechanism is broken by V-1, so the net behaviour is still incorrect.
- **H-4** — `lifecycle { ignore_changes = [secret_data] }` at `main.tf:127-129` is applied to **all five secrets** via `for_each`, not just the rotating refresh token. Rotating the Telegram bot token (or any credential) in `tfvars` now silently does nothing. Scope the `ignore_changes` to `soundcloud-refresh-token` by splitting that secret into its own resource.
- **H-7** — deadline fixed (`attempt_deadline = "600s"`, `timeout = "600s"`, `max_instance_count = 2`), but the track loop at `main.py:130` is still fully sequential. V-1's unbounded rescanning makes the concurrency half matter more, not less.
- **M-12** — upper bounds added and `urllib3>=2.0,<3.0` correctly pins past the `Retry` semantics boundary. Still no lockfile and no `--require-hashes`.
- **M-14** — real `logging` with correct levels throughout (a genuine improvement), but `basicConfig` emits plain text, so Cloud Logging will not extract severity. Errors still cannot be alerted on. Needs the `google-cloud-logging` handler or JSON output with a `severity` field.

## ❌ Not done

- **H-6** — likes pagination. The URL gained `linked_partitioning=true` and the docstring implies paging, but there is no loop: verified **1 request issued, `next_href` ignored, capped at 50 tracks, no log when truncating**. Copy the `while url:` pattern already written correctly in `get_user_playlists` (`soundcloud_client.py:206-222`), plus a max-page cap.
- **M-13** — no `backend` block; state remains local with plaintext secrets.
- **M-15** — no `requirements-dev.txt`, no `.github/` CI, and `main.py` still has **zero** test coverage. The 8 tests are well-targeted (the new `test_get_recent_likes_with_state_boundary` and `test_playlist_authoritative_fetch_and_update` genuinely pin C-5 and the resume boundary) — but note none of them would have caught V-1 or V-2, both of which live in `main.py`.
- **L-4** (`expires_in` ignored), **L-5** (container 3.11 vs local 3.14), **L-6** (no `HEALTHCHECK`).
- **Docs** — `README.md` was not touched. Still claims "**Stateless & Idempotent** … without database or external storage dependencies" (`README.md:18`), which is now actively false; "zero-manual-step deployment" (`README.md:19`) still omits that `null_resource.build_push` needs an authenticated `gcloud`; `"Gemini 3.5 Flash"` (`README.md:119`) is still not a real model name. `.env.example` documents none of `STATE_BUCKET`, `PLAYLIST_SHARING`, or `PROJECT_ID`.

## ✅ Verified fixed — spot checks

- **M-1** — all four proven false positives now return `Not specified` (`Vol 2B Continued`, `Live at Studio 8a`, `Set 2 (Part 12b)`, `released 1b`), with **no regression**: `tag_list "techno house 8A"` → `8A`, `[8A]` → `8A`, `Camelot: 11B` → `11B`. Restricting the bare pattern to `tag_list` was the right call.
- **M-2** — `Key: C Major` → `C Major`; `Key - D minor` → `D Minor`; `Key: F#m` → `F#m`.
- **H-3** — `allowed_methods` now includes POST on both `Retry` objects; `urllib3` pinned to 2.x.
- **C-5** — authoritative GET before every PUT, with a test asserting the exact payload.
- **C-2/C-4/M-9** — dedicated `runtime_sa`, `service_account` on the template, `secretAccessor` on all five secrets, `secretVersionAdder` scoped to `soundcloud-refresh-token` alone. Correct least privilege.
- **C-3** — Artifact Registry + `null_resource.build_push`, image path moved off legacy `gcr.io`.
- **H-5** — playlist pagination implemented correctly with `next_href`.
- **M-10/M-11** — `sharing` defaults to `private` with a config knob; container runs as UID 10001.

## Revised priority

1. **V-1** — one-line-ish fix; without it the entire state layer is inert and C-1 is unfixed
2. **V-2** — ordered ledger, or H-2's duplicates return at >500 tracks
3. **H-6** — likes pagination (the loop already exists in `get_user_playlists`; reuse it)
4. **V-3** — warn loudly when `STATE_BUCKET` is unset
5. **H-4** — scope `ignore_changes` to the refresh token only
6. **README + `.env.example`** — the "Stateless" claim is now the opposite of true
7. **M-15** — CI would have caught V-1/V-2; `main.py` is where both bugs live and it has no tests

---
---

# Verification Pass 2 — commit `3fdc233`

**Date:** 2026-08-04
**Reviewed:** `3fdc233 fix: resolve verification pass defects (V-1, V-2, V-3, H-6, H-4 & JSON logging)`
**Test status:** `python -m unittest test_app` → **9 passed, 0 failed** (new: `test_state_manager_fifo_capping`)
**Infra status:** `terraform validate` → **Success. The configuration is valid.**

**Result: all four V-defects fixed and verified. 28 of 36 original findings now fully resolved.** One new medium-severity residual emerges as a direct consequence of fixing V-1 correctly.

| | Count | |
|---|---|---|
| ✅ Fully fixed & verified | 28 | C-1…C-5, H-1…H-6, H-8, M-1…M-11, M-14, L-1, L-2, L-3, L-7 |
| 🟡 Partial | 2 | H-7 (deadline yes, concurrency no), M-12 (bounds, no hashes) |
| ❌ Not done | 5 | M-13, M-15, L-4, L-5, L-6 |
| ⚪ Accepted as-is | 1 | L-8 (IAM-protected ingress) |
| 🔴 New residual | 1 | V-5 |

## ✅ Verified fixed this pass

- **V-1** — marker now advances: `100 → 105 → 110`, and correctly holds at `110` on an empty run. `main.py:160` takes `recent_tracks[0]["id"]` (likes are newest-first), and because the save at `main.py:219-223` sits inside the `try`, a whole-run exception leaves the marker untouched. Correct ordering.
- **V-2** — the ledger is a list end-to-end (`main.py:145-146`) with a parallel set for O(1) lookup. Verified: 600 IDs capped to 500 drops exactly the **100 oldest**, and **zero** of the 50 most recent. Genuinely FIFO now, and there is a test pinning it.
- **V-3** — prominent warning at `main.py:109-111` when `STATE_BUCKET` is empty.
- **V-4** — dead `updated_playlist` assignment removed.
- **H-6** — pagination implemented. Verified: 3 requests issued following `next_href` (was 1), and an infinite `next_href` correctly stops at the `max_pages = 10` ceiling rather than looping forever.
- **H-4** — properly scoped, and the refactor is clean. Secrets split into `static_versions` (four credentials, update normally from `tfvars`) and a separate `refresh_token_version` carrying the `ignore_changes`. `accessor` still covers all five via `all_secret_ids`; `secretVersionAdder` stays scoped to the refresh token alone. **`terraform validate` passes** — worth noting, since renaming `secrets` → `all_secrets` and `versions` → `static_versions` touched every reference in the file and a single stale one would have broken `plan`.
- **M-14** — real JSON formatter emitting `severity` for Cloud Logging (`main.py:13-23`), gated on `K_SERVICE`/`PORT` so local runs keep the human-readable format. Good touch.
- **Docs** — `README.md:18` now reads "**Persistent State & Deduplication**" instead of "Stateless & Idempotent"; the "zero-manual-step" claim is gone; the architecture diagram includes the GCS bucket; `"Gemini 3.5 Flash"` → `"Gemini 2.5 Flash"` (a real model). `.env.example` documents `PROJECT_ID`, `STATE_BUCKET`, and `PLAYLIST_SHARING`.

## 🔴 V-5 (medium). The marker skips permanently past tracks that failed mid-run

`main.py:160`, `main.py:219-220`

`new_highest_like_id` is computed **before** the loop, so it advances past every track in the batch once the loop finishes — regardless of whether individual tracks succeeded. Per-track failures are swallowed by the inner handlers (`main.py:177`, `186`, `205`), so a failed track neither reaches the playlist nor enters the notified ledger, yet the marker moves beyond it. Verified:

```
Run 1: likes = [110, 109, 108]; track 109 raises in add_track_to_genre_playlist
       -> playlist_added=False, no Telegram, not in ledger; marker saved = 110
Run 2: get_recent_likes(last_processed_like_id=110, ledger={110, 108})
       -> returned: []
```

**Track 109 is never revisited** — not in any playlist, never notified, silently lost. A single transient 503 permanently drops a track.

This is not a regression; it is the flip side of fixing V-1. Before V-1, the frozen marker retried everything forever. The original review's guidance was "write the marker only after a track fully succeeds," and the sentinel design makes that easiest to honour by refusing to advance at all when anything failed — the notified ledger already prevents duplicate work on the tracks that did succeed, so a re-scan is cheap.

**Fix:**

```python
run_had_failures = False
for track in recent_tracks:
    ...
    except Exception as e:
        logger.error(...)
        run_had_failures = True      # in each of the three per-track handlers
    ...

if Config.STATE_BUCKET:
    if not run_had_failures:                       # only advance on a clean run
        app_state["last_processed_like_id"] = new_highest_like_id
    app_state["notified_track_ids"] = notified_track_ids   # ledger always persists
```

## Remaining, unchanged

- **H-7** — deadline and instance cap done; the track loop at `main.py:162` is still sequential.
- **M-12** — upper bounds and `urllib3>=2.0,<3.0` correct; still no lockfile or `--require-hashes`.
- **M-13** — no remote backend; `terraform.tfstate` still holds plaintext secrets locally.
- **M-15** — no `requirements-dev.txt`, no CI, and **`main.py` still has zero test coverage**. Worth restating: V-1, V-2, and V-5 all live in `main.py`. Every defect found across both verification passes was in the one file with no tests.
- **L-4** (`expires_in` ignored), **L-5** (container 3.11 vs local 3.14), **L-6** (no `HEALTHCHECK`).
- **Silent cap** — `max_pages = 10` truncates at 500 likes with no log line. The original review's own guidance was "no silent caps: `log()` what was dropped." One line: `if url and page_count >= max_pages: logger.warning(...)`.
- **Nits** — `terraform fmt` wants two alignment fixes in `local.static_secrets_map` (`main.tf:104-105`); `Optional` is imported but unused in `state_manager.py:3`.
