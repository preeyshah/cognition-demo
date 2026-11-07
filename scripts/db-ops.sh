#!/usr/bin/env bash
# Database monitoring, backups and redundancy for the Refunds Console Postgres.
#
#   scripts/db-ops.sh check [url]                Health report: version, uptime, connections, size, cache hit,
#                                                 replication, long queries, vacuum, backups (any Postgres)
#   scripts/db-ops.sh backup [url]               pg_dump (custom format) to ./backups, verified, keeps BACKUP_KEEP (7)
#   scripts/db-ops.sh restore-test <dump>        Restore a dump into a scratch database and verify row counts
#   scripts/db-ops.sh aws-harden <rds-id> [--replica]
#                                                 For any existing RDS instance: Multi-AZ, 14-day PITR, Enhanced
#                                                 Monitoring, Performance Insights, deletion protection, alarms,
#                                                 RDS event notifications, AWS Backup with a cross-region copy
#   scripts/db-ops.sh aws-status <rds-id>        Show the instance's redundancy/monitoring settings and alarm states
#
# No url = the local Compose database. Terraform-managed AWS deploys already get all of this (infra/db_resilience.tf).
set -euo pipefail
cd "$(dirname "$0")/.."
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
# shellcheck source=scripts/secrets.sh
source scripts/secrets.sh 2>/dev/null || true

NAME="${APP_NAME:-refunds-console}"
REGION="${AWS_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
PG_IMAGE=postgres:16-alpine
log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

libpq_url() { local u="${1/postgresql+psycopg:/postgresql:}"; echo "${u/postgres:/postgresql:}"; }

pg() {  # pg <url|""> <tool> [args...]  run a Postgres client tool against the target
  local url="$1"; shift
  if [[ -z "$url" ]]; then
    docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD:-refunds}" db "$@" -h localhost -U refunds -d refunds
  else
    docker run --rm -i --network host "$PG_IMAGE" "$@" -d "$(libpq_url "$url")"
  fi
}

check() {
  pg "${1:-}" psql -X -q -v ON_ERROR_STOP=1 <<'SQL'
\pset footer off
\echo '--- server'
SELECT version() AS version, now() - pg_postmaster_start_time() AS uptime, pg_is_in_recovery() AS is_replica;
\echo '--- connections'
SELECT count(*) AS used, current_setting('max_connections')::int AS max,
       round(100.0 * count(*) / current_setting('max_connections')::int, 1) AS pct,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
FROM pg_stat_activity;
\echo '--- size & cache'
SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size,
       round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS cache_hit_pct,
       xact_commit, xact_rollback, deadlocks, conflicts
FROM pg_stat_database WHERE datname = current_database();
\echo '--- replication (standbys attached to this server)'
SELECT client_addr, state, sync_state, replay_lag FROM pg_stat_replication;
\echo '--- queries running > 30s'
SELECT pid, now() - query_start AS runtime, state, left(query, 80) AS query
FROM pg_stat_activity WHERE state <> 'idle' AND now() - query_start > interval '30 seconds';
\echo '--- largest tables / vacuum'
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 8;
SQL
  local latest; latest=$(ls -1t backups/*.dump 2>/dev/null | head -1 || true)
  echo "--- local backups"; echo "${latest:-none} (${BACKUP_KEEP:-7} kept)"
}

backup() {
  mkdir -p backups; chmod 700 backups
  local f; f="backups/refunds-$(date -u +%Y%m%dT%H%M%SZ).dump"
  log "pg_dump -> $f"
  pg "${1:-}" pg_dump -Fc --no-owner > "$f"
  chmod 600 "$f"
  docker run --rm -i "$PG_IMAGE" pg_restore --list < "$f" >/dev/null || die "backup $f is unreadable"
  log "verified ($(du -h "$f" | cut -f1)); rotating to ${BACKUP_KEEP:-7}"
  ls -1t backups/*.dump | tail -n +"$((${BACKUP_KEEP:-7} + 1))" | xargs -r rm -f
}

restore_test() {
  local f="${1:?dump file}" c="refunds-restore-test-$$"
  log "restoring $f into a throwaway Postgres"
  docker run -d --rm --name "$c" -e POSTGRES_PASSWORD=t -e POSTGRES_DB=restore "$PG_IMAGE" >/dev/null
  trap 'docker rm -f "$c" >/dev/null 2>&1' RETURN
  for _ in $(seq 30); do docker exec "$c" pg_isready -U postgres -d restore >/dev/null 2>&1 && break; sleep 1; done
  sleep 1
  docker exec -i "$c" pg_restore --no-owner -U postgres -d restore < "$f"
  docker exec "$c" psql -X -U postgres -d restore -c \
    "SELECT (SELECT count(*) FROM refund_requests) AS refunds, (SELECT count(*) FROM audit_log) AS audit_rows, (SELECT count(*) FROM users) AS users;"
  log "restore OK"
}

aws_harden() {
  local id="${1:?rds instance id}" replica="${2:-}"
  command -v aws >/dev/null || die "AWS CLI v2 required"
  aws sts get-caller-identity >/dev/null || die "no AWS credentials"
  local acct arn
  acct=$(aws sts get-caller-identity --query Account --output text)
  arn=$(aws rds describe-db-instances --region "$REGION" --db-instance-identifier "$id" --query 'DBInstances[0].DBInstanceArn' --output text)

  log "Enhanced Monitoring role"
  aws iam create-role --role-name "$NAME-rds-monitoring" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"monitoring.rds.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null 2>&1 || true
  aws iam attach-role-policy --role-name "$NAME-rds-monitoring" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole
  sleep 10  # IAM propagation

  log "Multi-AZ standby, 14-day PITR, Performance Insights, deletion protection (applied in the next maintenance window unless APPLY_NOW=1)"
  aws rds modify-db-instance --region "$REGION" --db-instance-identifier "$id" \
    --multi-az --backup-retention-period 14 --copy-tags-to-snapshot --deletion-protection \
    --monitoring-interval 60 --monitoring-role-arn "arn:aws:iam::$acct:role/$NAME-rds-monitoring" \
    --enable-performance-insights --performance-insights-retention-period 7 \
    $([[ "${APPLY_NOW:-0}" == 1 ]] && echo --apply-immediately) >/dev/null

  log "Alerts topic + RDS event notifications"
  local topic
  topic=$(aws sns create-topic --region "$REGION" --name "$NAME-alerts" --query TopicArn --output text)
  [[ -n "${ALERT_EMAIL:-}" ]] && aws sns subscribe --region "$REGION" --topic-arn "$topic" --protocol email --notification-endpoint "$ALERT_EMAIL" >/dev/null
  aws rds create-event-subscription --region "$REGION" --subscription-name "$NAME-db-events" --sns-topic-arn "$topic" \
    --source-type db-instance --source-ids "$id" \
    --event-categories availability failover failure "low storage" maintenance recovery restoration >/dev/null 2>&1 || true

  log "CloudWatch alarms"
  alarm() {  # name metric stat op threshold
    aws cloudwatch put-metric-alarm --region "$REGION" --alarm-name "$NAME-db-$1" --namespace AWS/RDS --metric-name "$2" \
      --dimensions "Name=DBInstanceIdentifier,Value=$id" --statistic "$3" --comparison-operator "$4" --threshold "$5" \
      --period 300 --evaluation-periods 3 --alarm-actions "$topic" --ok-actions "$topic"
  }
  alarm cpu CPUUtilization Average GreaterThanThreshold 80
  alarm storage FreeStorageSpace Minimum LessThanThreshold 2147483648
  alarm memory FreeableMemory Minimum LessThanThreshold 104857600
  alarm connections DatabaseConnections Maximum GreaterThanThreshold 70
  alarm read-latency ReadLatency Average GreaterThanThreshold 0.05
  alarm write-latency WriteLatency Average GreaterThanThreshold 0.05

  log "AWS Backup: daily (35d) + weekly (180d), copied to $DR_REGION"
  aws backup create-backup-vault --region "$REGION" --backup-vault-name "$NAME-primary" >/dev/null 2>&1 || true
  aws backup create-backup-vault --region "$DR_REGION" --backup-vault-name "$NAME-dr" >/dev/null 2>&1 || true
  local dr_vault="arn:aws:backup:$DR_REGION:$acct:backup-vault:$NAME-dr" plan_id
  plan_id=$(aws backup list-backup-plans --region "$REGION" --query "BackupPlansList[?BackupPlanName=='$NAME-db'].BackupPlanId | [0]" --output text)
  if [[ "$plan_id" == "None" || -z "$plan_id" ]]; then
    plan_id=$(aws backup create-backup-plan --region "$REGION" --query BackupPlanId --output text --backup-plan "{
      \"BackupPlanName\": \"$NAME-db\",
      \"Rules\": [
        {\"RuleName\": \"daily\", \"TargetBackupVaultName\": \"$NAME-primary\", \"ScheduleExpression\": \"cron(0 5 * * ? *)\",
         \"Lifecycle\": {\"DeleteAfterDays\": 35}, \"CopyActions\": [{\"DestinationBackupVaultArn\": \"$dr_vault\", \"Lifecycle\": {\"DeleteAfterDays\": 35}}]},
        {\"RuleName\": \"weekly\", \"TargetBackupVaultName\": \"$NAME-primary\", \"ScheduleExpression\": \"cron(0 6 ? * SUN *)\",
         \"Lifecycle\": {\"DeleteAfterDays\": 180}, \"CopyActions\": [{\"DestinationBackupVaultArn\": \"$dr_vault\", \"Lifecycle\": {\"DeleteAfterDays\": 180}}]}
      ]}")
    aws iam create-service-linked-role --aws-service-name backup.amazonaws.com >/dev/null 2>&1 || true
    aws backup create-backup-selection --region "$REGION" --backup-plan-id "$plan_id" --backup-selection "{
      \"SelectionName\": \"$NAME-db\", \"Resources\": [\"$arn\"],
      \"IamRoleArn\": \"arn:aws:iam::$acct:role/aws-service-role/backup.amazonaws.com/AWSServiceRoleForBackup\"}" >/dev/null
  fi

  if [[ "$replica" == "--replica" ]]; then
    log "Read replica $id-replica"
    aws rds create-db-instance-read-replica --region "$REGION" --db-instance-identifier "$id-replica" \
      --source-db-instance-identifier "$id" --no-publicly-accessible \
      --monitoring-interval 60 --monitoring-role-arn "arn:aws:iam::$acct:role/$NAME-rds-monitoring" >/dev/null
  fi
  log "Done. Check with: scripts/db-ops.sh aws-status $id"
}

aws_status() {
  local id="${1:?rds instance id}"
  aws rds describe-db-instances --region "$REGION" --db-instance-identifier "$id" --output table --query \
    'DBInstances[0].{Status:DBInstanceStatus,MultiAZ:MultiAZ,SecondaryAZ:SecondaryAvailabilityZone,BackupDays:BackupRetentionPeriod,LatestRestorable:LatestRestorableTime,Encrypted:StorageEncrypted,DeletionProtection:DeletionProtection,MonitoringSec:MonitoringInterval,PerfInsights:PerformanceInsightsEnabled,Replicas:join(`,`,ReadReplicaDBInstanceIdentifiers),Pending:PendingModifiedValues}'
  aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix "$NAME-db-" --output table \
    --query 'MetricAlarms[].{Alarm:AlarmName,State:StateValue}'
  aws backup list-recovery-points-by-resource --region "$REGION" --output table --max-results 5 \
    --resource-arn "$(aws rds describe-db-instances --region "$REGION" --db-instance-identifier "$id" --query 'DBInstances[0].DBInstanceArn' --output text)" \
    --query 'RecoveryPoints[].{Created:CreationDate,Status:Status}' || true
}

case "${1:-}" in
  check) check "${2:-}" ;;
  backup) backup "${2:-}" ;;
  restore-test) restore_test "${2:-}" ;;
  aws-harden) aws_harden "${2:-}" "${3:-}" ;;
  aws-status) aws_status "${2:-}" ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
