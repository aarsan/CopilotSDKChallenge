# Plan: Require Full Pipeline for Auto-Onboarded Services

## Problem
`auto_onboard_service()` in `src/orchestrator.py` creates services with `status='approved'` and sets `active_version` — making them look fully validated when they never went through the 8-stage pipeline. Additionally, `set_active_service_version()` overwrites `reviewed_by` from `'orchestrator'` to `'Deployment Validated'`, hiding the auto-onboard origin.

## Approach
Change `auto_onboard_service()` to only do **prep work** (create service entry + ARM template + draft version) without approving. Use `status='not_approved'` with `reviewed_by='auto_prepped'` as a marker. Composition continues to work via draft version fallback.

## Changes

### 1. `src/orchestrator.py` — `auto_onboard_service()` (lines 29-166)
- Change `status: "approved"` → `"not_approved"` and `reviewed_by: "orchestrator"` → `"auto_prepped"`
- Remove the `set_active_service_version()` call (line 157) — no approval, no active version
- Change return from `"onboarded"` to `"prepped"`
- Add early return for already-prepped services (`reviewed_by == "auto_prepped"`)

### 2. `src/orchestrator.py` — `resolve_composition_dependencies()` (line 251)
- After the existing `status == "approved"` check, add a branch for prepped services (`reviewed_by == "auto_prepped"`) that fetches the draft version's ARM template
- Update the auto-onboard result check (line 285) to accept `"prepped"` and `"already_prepped"` as success

### 3. `src/orchestrator.py` — Second caller at line 699
- Update status check from `"onboarded"` to `"prepped"`/`"already_prepped"`

### 4. `src/web.py` — Co-onboard caller (~line 10501)
- Update status check to accept `"prepped"`/`"already_prepped"`

### 5. `src/web.py` — Composition endpoints (lines ~1114, ~4194, ~9152 + others)
- After `get_active_service_version()` returns `None` and before the `has_builtin_skeleton()` fallback, add a fallback to `get_latest_service_version()` to retrieve the draft ARM template

### 6. `src/pipelines/onboarding.py` (line 553)
- No change needed — it just creates the service entry then runs the full pipeline on it

### 7. `static/app.js` — `_renderOnboardButton()` (line 2229)
- Update the existing `isStub` check to also match `reviewed_by === 'auto_prepped'` so these services show "Needs Full Onboarding" with an onboard button
- The default `not_approved` catch-all (line 2359) already shows the onboard button, so this is a cosmetic enhancement

### 8. `static/app.js` — `_renderVersionedWorkflow()` (line 1937)
- Prepped services will now have `status='not_approved'` so they won't hit the `approved && !activeVersion` case — no change needed

### 9. Cache bust `static/index.html`

## What does NOT change
- The full onboarding pipeline (`POST /api/services/{id}/onboard`) — untouched
- `set_active_service_version()` — still used by the real pipeline
- `promote_service_after_validation()` — still used by the real pipeline
- Existing fully-validated services — unaffected
- The 5 already-auto-onboarded networking services stay as-is (they have `status='approved'` with `active_version` from the old code — users can re-validate them manually)
