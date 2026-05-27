#!/usr/bin/env bash
# Seed trusted_reporter labels into Osprey's PostgreSQL labels service.
#
# These pubkeys get immediate auto-hide authority for CSAM reports
# via reports/auto_hide.sml (TrustedReporterCSAM rule).
#
# Usage:
#   ./seed-trusted-reporters.sh              # local dev (port-forward assumed)
#   ./seed-trusted-reporters.sh --staging    # via kubectl exec into postgres pod
#
# To add a reporter: append their hex pubkey to TRUSTED_PUBKEYS below.

set -euo pipefail

# --- Trusted reporter pubkeys ---
# Format: "pubkey description"
TRUSTED_PUBKEYS=(
  # Admin (Matt)
  "81549bc0b5153b4b970fe4a3892ad185698b8b8b26ec69321a527d0644cd2898"
  # Moderation service identity
  "8fd5eb6d8f362163bc00a5ab6b4a3167dbf32d00ec4efdbcf43b3c9514433b7e"
)

# --- Label payload ---
# Matches EntityLabels.serialize() format from labels_service.py
LABEL_JSON='{"labels": {"trusted_reporter": {"added_at": null, "expires_at": null}}}'

generate_sql() {
  for pubkey in "${TRUSTED_PUBKEYS[@]}"; do
    entity_key="Pubkey/${pubkey}"
    cat <<SQL
INSERT INTO entity_labels (entity_key, labels)
VALUES ('${entity_key}', '${LABEL_JSON}')
ON CONFLICT (entity_key)
DO UPDATE SET labels = entity_labels.labels || '${LABEL_JSON}'::jsonb;
SQL
  done
}

if [[ "${1:-}" == "--staging" ]]; then
  echo "Seeding trusted_reporter labels on staging via kubectl..."
  SQL=$(generate_sql)
  kubectl exec -n osprey osprey-postgres-0 -- \
    psql -U osprey -d osprey -c "$SQL"
elif [[ "${1:-}" == "--dry-run" ]]; then
  echo "-- SQL that would be executed:"
  generate_sql
else
  echo "Seeding trusted_reporter labels on local dev..."
  SQL=$(generate_sql)
  psql -h localhost -U osprey -d osprey -c "$SQL"
fi

echo "Done. Seeded ${#TRUSTED_PUBKEYS[@]} trusted reporter(s)."
