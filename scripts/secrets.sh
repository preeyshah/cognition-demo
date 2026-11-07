#!/usr/bin/env bash
# Loads Refunds Console secrets (DEVIN_API_KEY, DEVIN_ORG_ID, JWT_SECRET, PROVIDER_API_KEY, ...) into the environment.
#
#   source scripts/secrets.sh          load: env already set > AWS Secrets Manager > encrypted local file > plain local file
#   scripts/secrets.sh init            create/replace the local store (prompts, nothing echoed; GPG-encrypted if gpg exists)
#   scripts/secrets.sh push            copy the local store into AWS Secrets Manager (then you can delete the local file)
#   scripts/secrets.sh status          show which source each secret came from (values are never printed)
#
# The local store lives OUTSIDE the repo at ~/.config/refunds-console/ (override with SECRETS_DIR) and is
# refused if anyone but you can read it. Nothing here ever writes a secret into the working tree or to stdout.

_rc_name="${APP_NAME:-refunds-console}"
_rc_dir="${SECRETS_DIR:-$HOME/.config/refunds-console}"
_rc_plain="$_rc_dir/secrets.env"
_rc_enc="$_rc_dir/secrets.env.gpg"
_rc_keys=(DEVIN_API_KEY DEVIN_ORG_ID JWT_SECRET PROVIDER_API_KEY PAYMENTS_WEBHOOK_SECRET POSTGRES_PASSWORD DATABASE_URL)
# Secrets Manager ids -> the JSON keys they hold (matches infra/data.tf)
_rc_sm_ids=("$_rc_name/devin-api-key" "$_rc_name/app")

_rc_log() { printf '\033[1;34m[secrets]\033[0m %s\n' "$*" >&2; }

_rc_set() {  # _rc_set KEY VALUE SOURCE  (never overrides something already exported, never sets empty values)
  [[ -n "$2" && -z "${!1:-}" ]] || return 0
  export "$1=$2"; export "_RC_SRC_$1=$3"
}

_rc_from_env_lines() {  # parse KEY=VALUE lines from stdin (no shell evaluation of the file)
  local src="$1" line k v
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    k="${line%%=*}"; v="${line#*=}"; k="${k//[[:space:]]/}"; k="${k#export}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ ]] && _rc_set "$k" "$v" "$src"
  done
}

_rc_perm_ok() {  # owner-only file in an owner-only dir
  local f="$1" mode
  mode=$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f")
  [[ "$mode" =~ ^[0-7]00$ && -O "$f" ]] || { _rc_log "refusing $f: permissions $mode (run: chmod 600 '$f')"; return 1; }
}

_rc_load_aws() {
  command -v aws >/dev/null && command -v python3 >/dev/null || return 0
  aws sts get-caller-identity >/dev/null 2>&1 || return 0
  local id json
  for id in "${_rc_sm_ids[@]}"; do
    json=$(aws secretsmanager get-secret-value --secret-id "$id" --query SecretString --output text 2>/dev/null) || continue
    _rc_from_env_lines "aws:$id" < <(printf '%s' "$json" | python3 -c \
      'import json,sys;[print(f"{k}={v}") for k,v in json.load(sys.stdin).items() if isinstance(v,str)]')
  done
  return 0
}

_rc_load_local() {
  if [[ -f "$_rc_enc" ]] && command -v gpg >/dev/null; then
    _rc_perm_ok "$_rc_enc" && _rc_from_env_lines "local:gpg" < <(gpg --quiet --decrypt "$_rc_enc" 2>/dev/null)
  fi
  if [[ -f "$_rc_plain" ]]; then
    _rc_perm_ok "$_rc_plain" && _rc_from_env_lines "local:plain" < "$_rc_plain"
  fi
  return 0
}

rc_load_secrets() {
  for k in "${_rc_keys[@]}"; do [[ -n "${!k:-}" ]] && export "_RC_SRC_$k=env"; done
  _rc_load_aws
  _rc_load_local
  local loaded=() k
  for k in "${_rc_keys[@]}"; do [[ -n "${!k:-}" ]] && loaded+=("$k"); done
  _rc_log "loaded: ${loaded[*]:-none}"
}

_rc_init() {
  umask 077; mkdir -p "$_rc_dir"; chmod 700 "$_rc_dir"
  local tmp k v; tmp=$(mktemp "$_rc_dir/.new.XXXXXX")
  trap 'rm -f "$tmp"' RETURN
  for k in "${_rc_keys[@]}"; do
    read -r -s -p "$k (blank to skip): " v </dev/tty; echo >&2
    [[ -n "$v" ]] && printf '%s=%s\n' "$k" "$v" >> "$tmp"
  done
  if command -v gpg >/dev/null; then
    gpg --yes --symmetric --cipher-algo AES256 --output "$_rc_enc" "$tmp" && chmod 600 "$_rc_enc"
    rm -f "$_rc_plain"; _rc_log "saved encrypted store $_rc_enc (passphrase-protected)"
  else
    mv "$tmp" "$_rc_plain"; chmod 600 "$_rc_plain"
    _rc_log "gpg not found: saved $_rc_plain (mode 600). Prefer 'scripts/secrets.sh push' to move it to AWS."
  fi
}

_rc_push() {
  aws sts get-caller-identity >/dev/null || { _rc_log "no AWS credentials"; return 1; }
  unset _RC_SRC_DEVIN_API_KEY; _rc_load_local
  [[ -n "${DEVIN_API_KEY:-}" ]] || { _rc_log "no DEVIN_API_KEY to push"; return 1; }
  # Key and org id share the secret the ECS task reads (infra/compute.tf), so both reach the API.
  # Merge: only overwrite values present locally, so a key-only push keeps the stored DEVIN_ORG_ID.
  { aws secretsmanager get-secret-value --secret-id "$_rc_name/devin-api-key" --query SecretString --output text 2>/dev/null || echo '{}'; } \
    | python3 -c 'import json,os,sys
cur=json.loads(sys.stdin.read() or "{}")
cur.update({k:os.environ[k] for k in ("DEVIN_API_KEY","DEVIN_ORG_ID") if os.environ.get(k)})
cur.setdefault("DEVIN_ORG_ID","")
print(json.dumps(cur),end="")' \
    | aws secretsmanager put-secret-value --secret-id "$_rc_name/devin-api-key" --secret-string file:///dev/stdin >/dev/null
  _rc_log "DEVIN_API_KEY + DEVIN_ORG_ID -> $_rc_name/devin-api-key"
  _rc_log "ECS reads secrets at task start: aws ecs update-service --cluster $_rc_name --service api --force-new-deployment"
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  rc_load_secrets  # sourced
else
  set -euo pipefail
  case "${1:-status}" in
    init) _rc_init ;;
    push) _rc_push ;;
    status) rc_load_secrets; for k in "${_rc_keys[@]}"; do s="_RC_SRC_$k"; printf '%-18s %s\n' "$k" "${!s:-missing}"; done ;;
    *) sed -n '2,10p' "$0"; exit 1 ;;
  esac
fi
