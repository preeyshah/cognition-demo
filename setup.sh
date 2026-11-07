#!/usr/bin/env bash
# Refunds Console setup. Runs the same app against any PostgreSQL:
#   ./setup.sh local                 Docker Compose: Postgres + API + provider + UI on this machine (default)
#   ./setup.sh external <url>        Use any hosted Postgres (Neon, Supabase, RDS/Aurora, Cloud SQL, Azure, ...)
#   ./setup.sh dev [url]             No containers for the app: venv + uvicorn + Vite (Postgres via Docker if no url)
#   ./setup.sh aws                   Provision AWS (VPC, WAF, RDS, ECS, CloudFront) with Terraform
#   ./setup.sh inspect [url]         Show tables / row counts / views to prove the data is in Postgres
#   ./setup.sh down                  Stop the local stack and delete its volume
set -euo pipefail
cd "$(dirname "$0")"
load_dotenv() {  # KEY=VALUE lines like Compose: no shell evaluation, never overrides exported vars
  [[ -f .env ]] || return 0
  local line k v
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    k="${line%%=*}"; v="${line#*=}"; k="${k//[[:space:]]/}"; k="${k#export}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && -z "${!k+x}" ]] && export "$k=$v"
  done < .env
}
load_dotenv
# DEVIN_API_KEY etc.: env > AWS Secrets Manager > ~/.config/refunds-console (see scripts/secrets.sh)
[[ "${1:-}" == down ]] || source scripts/secrets.sh
pg_pw() {  # URL-encode the DB password in pure bash (local/down must work without Python)
  local LC_ALL=C s="${POSTGRES_PASSWORD:-refunds}" o="" c i  # LC_ALL=C: iterate UTF-8 bytes
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in [a-zA-Z0-9.~_-]) o+="$c" ;; *) printf -v c '%%%02X' "'$c"; o+="$c" ;; esac
  done
  printf '%s' "$o"
}

LOCAL_URL="postgresql+psycopg://refunds:$(pg_pw)@db:5432/refunds"  # the Compose Postgres
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing '$1' ($2)"; }

# postgres://... (what Neon/Supabase/Heroku hand out) -> SQLAlchemy psycopg3 URL, TLS on for remote hosts.
normalize_url() {
  local u="$1"
  u="${u/#postgres:\/\//postgresql+psycopg://}"
  u="${u/#postgresql:\/\//postgresql+psycopg://}"
  local host="${u#*://}"; host="${host##*@}"; host="${host%%[:/?]*}"
  if [[ "$host" != localhost && "$host" != 127.0.0.1 && "$host" != db && "$u" != *sslmode=* ]]; then
    [[ "$u" == *\?* ]] && u="$u&sslmode=require" || u="$u?sslmode=require"
  fi
  printf '%s' "$u"
}

backend_py() {  # run python in the backend image (no local Python needed)
  docker compose run --rm --no-deps -e DATABASE_URL="$1" -e LOG_LEVEL=WARNING backend python -c "$2"
}

init_db() {  # create schema + analytics views + audit trigger + seed (idempotent)
  log "Creating schema and seeding demo data"
  backend_py "$1" "from app.main import init_db; init_db(); print('schema ready')"
}

inspect_db() {
  backend_py "$1" "
from sqlalchemy import inspect, text
from app.db import engine
with engine.connect() as c:
    print('server :', c.execute(text('select version()')).scalar().split(',')[0])
    insp = inspect(engine)
    for t in sorted(insp.get_table_names()):
        print(f'table  : {t:22} {c.execute(text(f\"select count(*) from {t}\")).scalar():>6} rows')
    for v in sorted(insp.get_view_names()):
        print(f'view   : {v}')
"
}

wait_http() {
  for _ in $(seq 1 60); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 2; done
  die "timed out waiting for $1"
}

cmd="${1:-local}"; shift || true
case "$cmd" in
  local)
    need docker "https://docs.docker.com/get-docker/"
    log "Starting Postgres 16, API, mock provider and UI"
    docker compose up -d --build
    wait_http http://localhost:8000/health
    log "Ready: UI http://localhost:8080  API http://localhost:8000/docs  DB postgres://refunds@localhost:${DB_PORT:-5433}/refunds"
    ;;
  external)
    need docker "https://docs.docker.com/get-docker/"
    [[ $# -ge 1 ]] || die "usage: ./setup.sh external 'postgres://user:pass@host:5432/db'"
    url="$(normalize_url "$1")"
    init_db "$url"
    log "Starting API, provider and UI against the external database"
    DATABASE_URL="$url" docker compose up -d --build --no-deps provider backend frontend
    wait_http http://localhost:8000/health
    log "Ready: UI http://localhost:8080 (data lives in your hosted Postgres)"
    ;;
  dev)
    need python3 "3.10+"; need npm "Node 20+"
    url="${1:-}"
    if [[ -z "$url" ]]; then
      need docker "or pass a Postgres URL"
      docker compose up -d --wait db  # same database that `./setup.sh inspect` reads
      url="postgresql+psycopg://refunds:$(pg_pw)@localhost:${DB_PORT:-5433}/refunds"
    else
      url="$(normalize_url "$url")"
    fi
    (cd backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt)
    (cd frontend && npm ci --silent)
    log "Starting provider :9000, API :8000, Vite :5173 (Ctrl-C stops all)"
    trap 'kill 0' EXIT
    (cd backend && .venv/bin/uvicorn mock_provider.main:app --port 9000) &
    (cd backend && DATABASE_URL="$url" .venv/bin/uvicorn app.main:app --port 8000) &
    (cd frontend && VITE_API_URL=http://localhost:8000 npm run dev -- --port 5173) &
    wait
    ;;
  aws)
    need terraform "https://developer.hashicorp.com/terraform/install"; need aws "AWS CLI v2"
    aws sts get-caller-identity >/dev/null || die "no AWS credentials (export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_REGION)"
    region="${AWS_REGION:-us-east-1}"
    log "Bootstrapping Terraform state + GitHub OIDC deploy role"
    (cd infra/bootstrap && terraform init -input=false && terraform apply -input=false -auto-approve -var region="$region")
    bucket=$(terraform -chdir=infra/bootstrap output -raw state_bucket)
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
      gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform -chdir=infra/bootstrap output -raw deploy_role_arn)"
      gh variable set TF_STATE_BUCKET --body "$bucket"; gh variable set AWS_REGION --body "$region"
      log "GitHub variables set: every push to main now deploys automatically"
    else
      log "gh not installed/authenticated: set repo variables AWS_DEPLOY_ROLE_ARN, TF_STATE_BUCKET, AWS_REGION manually"
    fi
    log "Running the first deploy (same steps as .github/workflows/deploy.yml)"
    (cd infra && terraform init -input=false -backend-config="bucket=$bucket" -backend-config="region=$region" \
      && terraform apply -input=false -auto-approve -var region="$region" -target=aws_ecr_repository.backend)
    repo=$(terraform -chdir=infra output -raw ecr_repository_url); tag=$(git rev-parse HEAD)
    aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${repo%%/*}"
    if ! aws ecr describe-images --region "$region" --repository-name "${repo#*/}" --image-ids imageTag="$tag" >/dev/null 2>&1; then
      docker build -t "$repo:$tag" backend && docker push "$repo:$tag"  # tags are immutable; skip on re-runs
    fi
    (cd infra && terraform apply -input=false -auto-approve -var region="$region" -var image_tag="$tag")
    (cd frontend && npm ci --silent && npm run build \
      && aws s3 sync dist "s3://$(terraform -chdir=../infra output -raw frontend_bucket)" --delete \
      && aws cloudfront create-invalidation --distribution-id "$(terraform -chdir=../infra output -raw cloudfront_distribution_id)" --paths '/*' >/dev/null)
    if [[ -n "${DEVIN_API_KEY:-}" && "${_RC_SRC_DEVIN_API_KEY:-}" != aws:* ]]; then
      scripts/secrets.sh push  # move the locally held key into Secrets Manager, then roll the API tasks
      aws ecs update-service --region "$region" --cluster "$(terraform -chdir=infra output -raw ecs_cluster)" \
        --service api --force-new-deployment >/dev/null
    fi
    log "Ready: $(terraform -chdir=infra output -raw app_url)"
    ;;
  inspect)
    need docker "https://docs.docker.com/get-docker/"
    inspect_db "$(normalize_url "${1:-$LOCAL_URL}")"
    ;;
  down)
    docker compose down -v
    ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
