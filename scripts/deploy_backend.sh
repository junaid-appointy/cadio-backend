#!/usr/bin/env bash
# CADIO backend deploy for the 3GB Bifrost node — automates the "size-flip"
# workaround (docs/reliability.md): the node can't hold two `heavy` (2048MB)
# pods at once, so a rolling update wedges. Until the platform gives this
# service `strategy: Recreate`, we deploy small, then flip back to heavy:
#
#   1. shrink the service to `standard` (512MB) — the new pod fits BESIDE the
#      old heavy pod, rollout succeeds, old pod terminates.
#      (The 512MB pod serves chat/metadata but refuses builds: the backend
#      boots in "constrained mode" below CADIO_MIN_BUILD_MEM_MB. Keep this
#      window short.)
#   2. flip back to `heavy` and deploy again — the 2048MB pod fits beside the
#      0.5GB pod, rollout succeeds.
#
# Trust /healthz at every step, not CLI exit codes: the platform has reported
# rollouts as `failed` that were actually fine (see docs/reliability.md).
set -euo pipefail

SERVICE="${CADIO_BIFROST_SERVICE:-backend}"
ENVIRONMENT="${CADIO_BIFROST_ENV:-dev}"
BACKEND_URL="${CADIO_BACKEND_URL:-https://backend-dev-226257.bifrost.saastack.site}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-420}"

say() { printf '\n==> %s\n' "$*"; }

healthz() { curl -fsS --max-time 10 "$BACKEND_URL/healthz" 2>/dev/null || true; }

# wait_limit <expected_limit_mb>: poll /healthz until mem.limit_mb matches.
wait_limit() {
  local want="$1" deadline=$((SECONDS + POLL_TIMEOUT_S)) body limit
  while (( SECONDS < deadline )); do
    body="$(healthz)"
    limit="$(printf '%s' "$body" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("mem",{}).get("limit_mb",""))
except Exception: print("")' 2>/dev/null || true)"
    if [[ "$limit" == "$want" ]]; then
      say "healthz reports limit_mb=$limit ✓"
      return 0
    fi
    printf '.'
    sleep 5
  done
  echo
  echo "FATAL: /healthz never reported limit_mb=$want within ${POLL_TIMEOUT_S}s" >&2
  echo "last healthz: $(healthz)" >&2
  return 1
}

# `bifrost deploy` inside a git repo injects the LOCAL commit sha (and fails
# clone on unpushed ones) — run it from a scratch dir.
deploy() {
  say "deploy $SERVICE ($ENVIRONMENT)"
  (cd "$(mktemp -d)" && bifrost deploy --service "$SERVICE" --environment "$ENVIRONMENT") \
    || echo "note: deploy command reported failure — verifying via /healthz instead"
}

say "preflight: current /healthz"
healthz || true
echo

say "step 1/2: shrink to standard (512MB) so the new pod fits beside the old heavy pod"
bifrost service update "$SERVICE" --compute-size standard
deploy
wait_limit 512

say "step 2/2: flip back to heavy (2048MB)"
bifrost service update "$SERVICE" --compute-size heavy
deploy
wait_limit 2048

say "smoke: /healthz must show a fresh, unconstrained engine"
body="$(healthz)"
echo "$body"
printf '%s' "$body" | python3 -c 'import json,sys
h = json.load(sys.stdin)
assert h.get("ok") is True, "healthz not ok"
eng = h.get("engine") or {}
assert not eng.get("constrained"), "engine still in constrained mode!"
peak = (h.get("mem") or {}).get("rss_peak_mb")
assert peak is None or peak < 600, f"rss_peak_mb={peak} — not a fresh pod?"
print("smoke OK: unconstrained engine, fresh pod")'

say "deploy complete"
