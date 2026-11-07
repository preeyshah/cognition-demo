#!/usr/bin/env bash
# Self-healing demo: a planted bug on the `demo` branch -> 500 -> error ticket -> Devin session -> fix PR into `demo`.
#
#   scripts/demo.sh reset     rebuild `demo` = origin/dev (or $DEMO_BASE) + planted bug, force-push it, close open PRs into `demo`
#   scripts/demo.sh up        run the `demo` branch with Docker Compose (fresh DB), PRs target `demo`  -> http://localhost:8080
#   scripts/demo.sh trigger   reproduce from the CLI: add a new reviewer, then load Analytics (-> 500 + ticket)
#   scripts/demo.sh status    list support tickets (status + Devin session link)
#   scripts/demo.sh down      stop the demo stack and delete its volume
#
# `dev` and `main` are never touched. The demo stack runs from a git worktree in .demo/ (gitignored).
# DEVIN_API_KEY comes from the env, AWS Secrets Manager or ~/.config/refunds-console (see scripts/secrets.sh);
# without it tickets are recorded but nothing is sent to Devin.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
WT="$ROOT/.demo"
BRANCH="${DEMO_BRANCH:-demo}"
BASE="${DEMO_BASE:-dev}"  # branch the demo is built from
PROJECT=refunds-demo
API="${DEMO_API:-http://localhost:8000}"
ADMIN=priya.admin@acmepay.com

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
[[ "$BRANCH" != main && "$BRANCH" != master && "$BRANCH" != dev ]] || die "refusing to use $BRANCH as the demo branch"

worktree_at() {  # check out $1 (detached) in .demo/, discarding anything left over from the last run
  git fetch -q origin "$2"
  if [[ -d "$WT/.git" || -f "$WT/.git" ]]; then
    git -C "$WT" reset -q --hard && git -C "$WT" clean -qfd && git -C "$WT" checkout -q --detach "$1"
  else
    git worktree add -q --detach "$WT" "$1"
  fi
}

plant_bug() {  # a plausible "simplification" that divides by zero for anyone with no decisions in the window
  python3 - "$WT/backend/app/analytics.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = '"approval_rate": _rate(len(ok), len(mine)),'
new = '"approval_rate": round(len(ok) / len(mine), 3),'
if old not in s:
    sys.exit("analytics.py changed upstream: update plant_bug in scripts/demo.sh")
open(p, "w").write(s.replace(old, new))
PY
}

cmd_reset() {
  worktree_at "origin/$BASE" "$BASE"
  plant_bug
  git -C "$WT" commit -q -am "Analytics: simplify per-reviewer approval rate"
  git -C "$WT" push -q --force origin "HEAD:refs/heads/$BRANCH"
  log "origin/$BRANCH = origin/$BASE + planted bug ($(git -C "$WT" rev-parse --short HEAD))"
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    for pr in $(gh pr list --base "$BRANCH" --state open --json number --jq '.[].number'); do
      gh pr close "$pr" --comment "Closed by scripts/demo.sh reset (demo branch rebuilt)." >/dev/null && log "closed PR #$pr"
    done
  fi
}

cmd_up() {
  git ls-remote --exit-code -q origin "refs/heads/$BRANCH" >/dev/null || die "origin/$BRANCH missing: run scripts/demo.sh reset"
  worktree_at "origin/$BRANCH" "$BRANCH"
  source scripts/secrets.sh
  [[ -n "${DEVIN_API_KEY:-}" ]] || log "DEVIN_API_KEY not set: tickets will be recorded but not sent to Devin"
  export DEVIN_BASE_BRANCH="$BRANCH" JWT_SECRET="${JWT_SECRET:-demo-$(date +%s)-$RANDOM}"
  (cd "$WT" && docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
   log "Building and starting the demo stack (log: .demo/compose.log)"
   docker compose -p "$PROJECT" up --build -d --wait >compose.log 2>&1 || { tail -20 compose.log; exit 1; })
  log "Demo running from $BRANCH @ $(git -C "$WT" rev-parse --short HEAD): http://localhost:8080 (sign in as $ADMIN)"
  log "Story: Team & limits -> add a reviewer -> Analytics fails with a trace id -> Support tickets -> Open session"
}

token() {  # mock-SSO login as the admin (authorize -> MFA -> authorization code -> bearer token)
  local loc code
  curl -fsS -o /dev/null -X POST "$API/auth/sso/authorize" \
    --data-urlencode "email=$ADMIN" --data-urlencode "redirect_uri=http://localhost:8080/"
  loc=$(curl -fsS -o /dev/null -w '%{redirect_url}' -X POST "$API/auth/sso/mfa" \
    --data-urlencode "email=$ADMIN" --data-urlencode "redirect_uri=http://localhost:8080/" \
    --data-urlencode "code=${MOCK_MFA_CODE:-123456}")
  code="${loc#*code=}"
  curl -fsS -X POST "$API/auth/token" -H 'Content-Type: application/json' -d "{\"code\":\"$code\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

cmd_trigger() {
  local t email
  t=$(token)
  email="new.reviewer.$(date +%s)@acmepay.com"
  curl -fsS -o /dev/null -X POST "$API/admin/users" -H "Authorization: Bearer $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$email\",\"name\":\"New Reviewer\",\"role\":\"reviewer\",\"approval_limit_cents\":50000,\"daily_approval_limit_cents\":500000}"
  log "added reviewer $email; loading Analytics"
  curl -sS -D - -o /dev/null "$API/analytics/overview?days=30" -H "Authorization: Bearer $t" | grep -iE '^(HTTP|x-trace-id)'
  sleep 2; cmd_status "$t"
}

cmd_status() {
  local t="${1:-$(token)}"
  curl -fsS "$API/admin/support/requests" -H "Authorization: Bearer $t" | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    link = r["devin_session_url"] or r["remediation_error"] or ""
    print("#{:<3} {:<7} {:<18} {:<60} {}".format(r["id"], r["kind"], r["status"], r["title"][:60], link))'
}

cmd_down() {
  [[ -d "$WT" ]] && (cd "$WT" && docker compose -p "$PROJECT" down -v --remove-orphans)
}

case "${1:-}" in
  reset) cmd_reset ;; up) cmd_up ;; trigger) cmd_trigger ;; status) cmd_status ;; down) cmd_down ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
