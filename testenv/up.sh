#!/usr/bin/env bash
# Local test environment: the real backend + dashboard against a filesystem
# "bucket" — no HF org, tokens, or Spaces. See testenv/README.md.
#
#   ./testenv/up.sh            # start both (backend :8100, dashboard :7861)
#   ./testenv/up.sh --reset    # wipe state first, then start (re-seeds)
#
# Env overrides: TESTENV_USER (default: tester), TESTENV_STATE, UV_INDEX_URL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${TESTENV_STATE:-$ROOT/.testenv}"
DEV_USER="${TESTENV_USER:-tester}"
# The org-internal uv index is VPN-only; default to public PyPI.
export UV_INDEX_URL="${UV_INDEX_URL:-https://pypi.org/simple}"
BACKEND_PORT="${TESTENV_BACKEND_PORT:-8100}"
DASH_PORT="${TESTENV_DASH_PORT:-7861}"
ORG="local-org"
SLUG="collab"
CENTRAL="$STATE/buckets/$ORG/$SLUG-main-bucket"

for p in "$BACKEND_PORT" "$DASH_PORT"; do
  if lsof -ti "tcp:$p" >/dev/null 2>&1; then
    echo "!! port $p is already in use — a testenv is probably running." >&2
    # lsof takes one -i per port; `lsof -ti tcp:a tcp:b` reads the second as a
    # filename and silently matches nothing.
    echo "   stop it first: lsof -ti tcp:$BACKEND_PORT -i tcp:$DASH_PORT | xargs kill" >&2
    exit 1
  fi
done

if [[ "${1:-}" == "--reset" ]]; then
  echo "wiping $STATE"
  rm -rf "$STATE"
fi
SEED=""
[[ -d "$CENTRAL" ]] || SEED="--seed"
mkdir -p "$STATE"

echo "── backend  http://127.0.0.1:$BACKEND_PORT  (log: $STATE/backend.log)"
(
  cd "$ROOT/backend" &&
  exec uv run --no-project --with-requirements requirements.txt --with pyyaml \
    python scripts/dev_server.py \
      --root "$STATE/buckets" --org "$ORG" --slug "$SLUG" \
      --user "$DEV_USER" --port "$BACKEND_PORT" $SEED
) >"$STATE/backend.log" 2>&1 &
BACKEND_PID=$!

echo "── dashboard http://127.0.0.1:$DASH_PORT  (log: $STATE/dashboard.log)"
(
  cd "$ROOT/dashboard" &&
  ORG="$ORG" \
  BUCKET="$ORG/$SLUG-main-bucket" \
  LOCAL_BUCKET_DIR="$CENTRAL" \
  BACKEND_API_URL="http://127.0.0.1:$BACKEND_PORT" \
  DEV_FAKE_LOGIN="$DEV_USER" \
  CHALLENGE_TITLE="Local Test Collab" \
  CHALLENGE_TAGLINE="testenv — nothing here is real" \
  exec uv run --no-project --with-requirements requirements.txt \
    python -m uvicorn app:app --host 127.0.0.1 --port "$DASH_PORT"
) >"$STATE/dashboard.log" 2>&1 &
DASH_PID=$!

cleanup() { kill "$BACKEND_PID" "$DASH_PID" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

# Wait for both to come up, then idle until Ctrl-C.
for i in $(seq 1 60); do
  ok=0
  curl -sf "http://127.0.0.1:$BACKEND_PORT/v1/healthz" >/dev/null 2>&1 && ok=$((ok+1))
  curl -sf "http://127.0.0.1:$DASH_PORT/api/health"    >/dev/null 2>&1 && ok=$((ok+1))
  [[ $ok -eq 2 ]] && break
  sleep 0.5
done
if [[ ${ok:-0} -ne 2 ]]; then
  echo "!! something didn't come up — check the logs in $STATE" >&2
  exit 1
fi

echo
echo "ready:"
echo "  dashboard  http://127.0.0.1:$DASH_PORT   (click the composer button to fake-login as '$DEV_USER')"
echo "  backend    http://127.0.0.1:$BACKEND_PORT/v1"
echo "  bucket     $CENTRAL"
echo
echo "Ctrl-C stops both."
wait
