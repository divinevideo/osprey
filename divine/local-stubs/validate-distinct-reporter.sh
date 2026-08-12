#!/usr/bin/env bash
# Proves the multi-report threshold counts DISTINCT REPORTERS, not reports.
#
# The threshold in multi_report_threshold.sml is a label-presence chain, because
# Osprey ships no counting primitive at all. Before the EventReporterId guard,
# that chain could not tell two reporters from one reporter reporting twice --
# the rule file said so itself, as its P2 defect.
#
# The discriminating sequence is A/B/C below. B is the whole point: under the
# old rules B satisfied the threshold and auto-hid the event on ONE person's
# say-so. A test that only checks "two reports enforce" passes either way and
# proves nothing.
set -euo pipefail

KAFKA_TOPIC="osprey.actions_input"

RUN_BASE=$(( ($(date +%s) % 100000) * 10 ))
A1=$((RUN_BASE + 1)); A2=$((RUN_BASE + 2)); A3=$((RUN_BASE + 3)); A4=$((RUN_BASE + 4))

# Run-scoped so a re-run cannot read a previous run's labels. Entity labels
# persist in Postgres, so a fixed target would be `sexual_reported` already and
# case A would silently behave like case C.
TARGET_EVENT="ee11$(printf '%060x' "$RUN_BASE")"
REPORTER_ONE="f1$(printf '%062x' "$RUN_BASE")"
REPORTER_TWO="f2$(printf '%062x' "$RUN_BASE")"

echo "== target event : $TARGET_EVENT"
echo "== reporter one : $REPORTER_ONE"
echo "== reporter two : $REPORTER_TWO"
echo "== action ids   : $A1 $A2 $A3 $A4"

send() {
  docker exec -i divine-kafka \
    kafka-console-producer --bootstrap-server kafka:29092 --topic "$KAFKA_TOPIC"
}

NOW=$(date +%s)
TS=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

report() {  # report <action_id> <reporter_pubkey> <created_at_offset> [reason]
  local reason="${4:-nudity}"
  cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$1,"action_name":"nostr_kind_1984","data":{"event_id":"aa$(printf '%062x' "$1")","pubkey":"$2","kind":1984,"created_at":$((NOW+$3)),"content":"","tags":[["e","$TARGET_EVENT"],["report","$reason"]],"sig":"test","reported_event_id":"$TARGET_EVENT","reported_pubkey":"","report_reason":"$reason"}}}
JSON
}

echo "== A: reporter ONE reports the event -> expect flag_for_review, no enforcement"
report $A1 "$REPORTER_ONE" 0
sleep 6

echo "== B: reporter ONE reports the SAME event again -> expect NOTHING"
report $A2 "$REPORTER_ONE" 1
sleep 6

echo "== C: reporter TWO reports it -> expect auto_hide (threshold met by 2 distinct people)"
report $A3 "$REPORTER_TWO" 2

sleep 6

# D: the SAME reporter, same event, DIFFERENT category.
#
# The dedup guard exists to stop one person advancing a threshold alone. It must
# not also stop them making a distinct accusation. If the reporter key is not
# category-scoped, one `reporter_counted` label blocks every category at once:
# this report matches neither the first-report rule (guard set) nor the
# threshold rule (that category has no prior report), so it matches NOTHING and
# is silently dropped -- no verdict, no COOP item, no telemetry.
#
# It also inflates the threshold: nudity would then need three distinct
# reporters whenever one of them had previously reported the event for anything
# else.
echo "== D: reporter ONE reports the SAME event under a DIFFERENT category -> expect a verdict"
report $A4 "$REPORTER_ONE" 3 violence

echo "== waiting for processing"
sleep 12

LOGS=$(docker logs divine-worker --since 5m 2>&1)
verdict_for() {
  { echo "$LOGS" | grep "\"__action_id\": $1," | head -1 \
    | grep -o '"__verdicts": \[[^]]*\]' | head -1; } || true
}
rules_for() {
  { echo "$LOGS" | grep "\"__action_id\": $1," | head -1 \
    | grep -o "RuleT(name='[^']*'" | sed "s/RuleT(name='//;s/'//" | tr '\n' ' '; } || true
}

echo
echo "== verdicts as recorded by the worker"
for id in $A1 $A2 $A3 $A4; do
  echo "  action $id: $(verdict_for $id)  rules: $(rules_for $id)"
done

fail=0
check() { if [ "$2" = "1" ]; then echo "  PASS  $1"; else echo "  FAIL  $1"; fail=1; fi; }

echo
echo "== assertions"

vA=$(verdict_for $A1); vB=$(verdict_for $A2); vC=$(verdict_for $A3); vD=$(verdict_for $A4)

check "A: first reporter produced a verdict at all" \
  "$([ -n "$vA" ] && echo 1 || echo 0)"
check "A: first report is flag_for_review, not enforcement" \
  "$(echo "$vA" | grep -q flag_for_review && echo 1 || echo 0)"
check "A: first report did NOT auto-hide" \
  "$(echo "$vA" | grep -q auto_hide && echo 0 || echo 1)"

# THE DISCRIMINATING ASSERTION. Under the pre-guard rules this was auto_hide:
# one person could hide an event by reporting it twice.
check "B: the SAME reporter reporting twice did NOT meet the threshold" \
  "$(echo "$vB" | grep -q auto_hide && echo 0 || echo 1)"
check "B: the SAME reporter's second report declared no enforcement at all" \
  "$(echo "$vB" | grep -qE 'auto_hide|ban|restrict' && echo 0 || echo 1)"

check "C: a SECOND DISTINCT reporter met the threshold" \
  "$(echo "$vC" | grep -q auto_hide && echo 1 || echo 0)"

# D is the counterpart to B. B proves the guard blocks a repeat of the SAME
# accusation; D proves it does not also swallow a DIFFERENT one.
check "D: a different category from the same reporter was not silently dropped" \
  "$([ -n "$vD" ] && echo 1 || echo 0)"
check "D: that report is flag_for_review, not enforcement on one person's say-so" \
  "$(echo "$vD" | grep -q flag_for_review && echo 1 || echo 0)"
check "D: that report did NOT auto-hide" \
  "$(echo "$vD" | grep -q auto_hide && echo 0 || echo 1)"

echo
if [ "$fail" = "0" ]; then
  echo "ALL ASSERTIONS PASSED"
else
  echo "FAILURES ABOVE"; exit 1
fi
