#!/usr/bin/env bash
# Local validation of the age-restrict effect (osprey#8), which could not be run
# on staging: the rule requires a label signed by the trusted moderation
# identity, and no such label has ever reached the staging relay. Making the
# trusted signer configurable is what lets a local run stand in.
#
# Exercises the case the PR's test plan asks for and nothing else covers:
# labels WITH and WITHOUT the sha256 the age-restrict path requires.
set -euo pipefail

KAFKA_TOPIC="osprey.actions_input"
STUB="http://127.0.0.1:8090"
MODSVC_WORKTREE="${MODSVC_WORKTREE:-$HOME/code/.worktrees/modsvc-dm-relay-config}"
TESTPUB="$(cat /tmp/testpub.txt)"

TARGET_EVENT="aa11$(printf '0%.0s' {1..59})1"   # 64 hex
TARGET_AUTHOR="bb22$(printf '0%.0s' {1..59})2"  # 64 hex, the real signer
CLAIMED_PUBKEY="cc33$(printf '0%.0s' {1..59})3" # 64 hex, what a reporter might claim

# ClickHouse keeps every run's rows, so fixed action ids make a re-run read a
# PREVIOUS run's verdict. Derive them per run instead. (This bit once: a run
# against stale rows reported the old rule set's behaviour.)
RUN_BASE=$(( ($(date +%s) % 100000) * 10 ))

# The media hash is run-scoped for the same reason. In stub mode the call log is
# reset per run so a fixed hash was survivable; in live mode the evidence is a D1
# row that persists, and a fixed hash would let a PREVIOUS run's row satisfy this
# run's assertion.
MEDIA_HASH="dd44$(printf '%060x' "$RUN_BASE")"    # 64 hex, case 1
# Case 4 gets its OWN hash. Cases 1 and 4 both enforcing on one hash is two
# entries in a call log but a single upserted row in D1 (sha256 is the primary
# key), so a shared hash makes "both cases enforced" unobservable in live mode.
# Distinct hashes also make the assertion stronger: each enforcement is
# attributed to its case rather than counted anonymously.
MEDIA_HASH_4="dd45$(printf '%060x' "$RUN_BASE")"  # 64 hex, case 4

# WHERE THE WORKER ACTUALLY SENDS ENFORCEMENT.
#
# This harness reads a call log to decide whether enforcement happened. If it
# reads a log the worker never writes to, the positive assertions fail for a
# reason that has nothing to do with the rules, and -- worse -- every "no call
# carried X" assertion passes VACUOUSLY, because an empty log contains no bad
# call either. Safety properties reported green on no evidence at all.
#
# So resolve the worker's real sink and pick the evidence source from it, rather
# than assuming.
WORKER_SINK="$(docker exec divine-worker printenv DIVINE_RELAY_MANAGER_URL 2>/dev/null || true)"
case "$WORKER_SINK" in
  *:8090*)  MODE=stub ;;
  *:8787*)  MODE=live ;;
  "")       echo "ABORT: cannot read DIVINE_RELAY_MANAGER_URL from divine-worker (container down?)" >&2; exit 2 ;;
  *)        echo "ABORT: worker posts to '$WORKER_SINK', which this harness cannot observe." >&2
            echo "       Point it at the stub (:8090) or the real relay-manager (:8787)." >&2
            exit 2 ;;
esac
echo "== worker enforcement sink: $WORKER_SINK  (mode: $MODE)"
A1=$((RUN_BASE + 1)); A2=$((RUN_BASE + 2)); A3=$((RUN_BASE + 3))
A4=$((RUN_BASE + 4)); A5=$((RUN_BASE + 5))
echo "== run action ids: $A1 $A2 $A3 $A4 $A5"

if [ "$MODE" = stub ]; then
  echo "== seeding the relay stub so the target event resolves to its real author"
  curl -sf -X PUT "$STUB/_seed" -H 'Content-Type: application/json' \
    -d "{\"$TARGET_EVENT\":\"$TARGET_AUTHOR\"}" >/dev/null
  curl -sf -X POST "$STUB/_reset" >/dev/null
else
  echo "== live mode: author resolution comes from real funnelcake, not the stub"
  echo "   (seeding is a no-op here; the target event need not exist for the"
  echo "    age-restrict path, which is addressed by hash)"
fi

send() {
  docker exec -i divine-kafka \
    kafka-console-producer --bootstrap-server kafka:29092 --topic "$KAFKA_TOPIC"
}

NOW=$(date +%s)
TS=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

echo "== case 1: confirmed-nudity label WITH sha256 -> expect age-restrict enforcement"
cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$A1,"action_name":"nostr_kind_1985","data":{"event_id":"e1$(printf '0%.0s' {1..61})1","pubkey":"$TESTPUB","kind":1985,"created_at":$NOW,"content":"","tags":[["L","content-warning"],["l","nudity","content-warning","{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH\"}"],["e","$TARGET_EVENT"]],"sig":"test","label_namespace":"content-warning","label_value":"nudity","label_source":"human-moderator","label_rejected":false,"label_target_event":"$TARGET_EVENT","label_content_hash":"$MEDIA_HASH","label_metadata":"{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH\"}"}}}
JSON

echo "== case 2: target event but NO sha256 -> expect NO enforcement, but a review verdict"
cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$A2,"action_name":"nostr_kind_1985","data":{"event_id":"e2$(printf '0%.0s' {1..61})2","pubkey":"$TESTPUB","kind":1985,"created_at":$((NOW+1)),"content":"","tags":[["L","content-warning"],["l","nudity","content-warning","{\"source\":\"human-moderator\"}"],["e","$TARGET_EVENT"]],"sig":"test","label_namespace":"content-warning","label_value":"nudity","label_source":"human-moderator","label_rejected":false,"label_target_event":"$TARGET_EVENT","label_metadata":"{\"source\":\"human-moderator\"}"}}}
JSON

echo "== case 3: label from an UNTRUSTED signer, with sha256 -> expect nothing"
cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$A3,"action_name":"nostr_kind_1985","data":{"event_id":"e3$(printf '0%.0s' {1..61})3","pubkey":"$CLAIMED_PUBKEY","kind":1985,"created_at":$((NOW+2)),"content":"","tags":[["L","content-warning"],["l","nudity","content-warning","{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH\"}"],["e","$TARGET_EVENT"]],"sig":"test","label_namespace":"content-warning","label_value":"nudity","label_source":"human-moderator","label_rejected":false,"label_target_event":"$TARGET_EVENT","label_content_hash":"$MEDIA_HASH","label_metadata":"{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH\"}"}}}
JSON

# The common real-world shape: the publisher sets the imeta x tag
# unconditionally and the e tag only when it has one. This case is why the
# rules key off the hash rather than the event.
echo "== case 4: sha256 but NO target event -> expect age-restrict enforcement"
cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$A4,"action_name":"nostr_kind_1985","data":{"event_id":"e4$(printf '0%.0s' {1..61})4","pubkey":"$TESTPUB","kind":1985,"created_at":$((NOW+3)),"content":"","tags":[["L","content-warning"],["l","nudity","content-warning","{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH_4\"}"]],"sig":"test","label_namespace":"content-warning","label_value":"nudity","label_source":"human-moderator","label_rejected":false,"label_content_hash":"$MEDIA_HASH_4","label_metadata":"{\"source\":\"human-moderator\",\"sha256\":\"$MEDIA_HASH_4\"}"}}}
JSON

# A malformed hash is not enforceable, so it must route to review like an absent
# one. Before IsValidMediaHash gated the rules, this declared `restrict` and wrote
# the age_restricted label while the sink skipped the call -- Osprey recording a
# restriction that never happened.
echo "== case 5: confirmed label with a MALFORMED sha256 -> expect review, no enforcement"
cat <<JSON | send
{"send_time":"$TS","data":{"action_id":$A5,"action_name":"nostr_kind_1985","data":{"event_id":"e5$(printf '0%.0s' {1..61})5","pubkey":"$TESTPUB","kind":1985,"created_at":$((NOW+4)),"content":"","tags":[["L","content-warning"],["l","nudity","content-warning","{\"source\":\"human-moderator\",\"sha256\":\"not-a-valid-sha256\"}"],["e","$TARGET_EVENT"],["x","not-a-valid-sha256"]],"sig":"test","label_namespace":"content-warning","label_value":"nudity","label_source":"human-moderator","label_rejected":false,"label_target_event":"$TARGET_EVENT","label_content_hash":"not-a-valid-sha256","label_metadata":"{\"source\":\"human-moderator\",\"sha256\":\"not-a-valid-sha256\"}"}}}
JSON

echo "== waiting for processing"
sleep 12

# Both modes produce the same shape -- a list of {path, body} -- so the
# assertions below read one format regardless of where the evidence came from.
if [ "$MODE" = stub ]; then
  CALLS=$(curl -s "$STUB/_calls")
else
  # Live: the durable record of an enforcement call is the moderation_results
  # row that /api/v1/moderate writes. That is a STRONGER assertion than the
  # stub's log -- it proves the call was accepted and recorded end to end,
  # not merely sent.
  [ -d "$MODSVC_WORKTREE" ] || {
    echo "ABORT: MODSVC_WORKTREE '$MODSVC_WORKTREE' does not exist; live mode reads its D1." >&2
    exit 2
  }
  CALLS=$( cd "$MODSVC_WORKTREE" && npx wrangler d1 execute BLOSSOM_DB --local --json \
      --command "SELECT sha256, action FROM moderation_results WHERE moderated_at > datetime('now','-10 minutes')" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    rows = json.load(sys.stdin)[0]['results']
except Exception:
    rows = []
print(json.dumps([
    {'path': '/api/moderate-media', 'body': {'sha256': r.get('sha256'), 'action': r.get('action')}}
    for r in rows
]))" )
fi

echo
echo "== what the worker asked relay-manager to do (source: $MODE)"
echo "$CALLS" | python3 -m json.tool

# A negative assertion over an EMPTY evidence set is not evidence of safety, it
# is absence of evidence. Every "no call carried X" check below would pass on a
# log nothing writes to, which is exactly how a misconfigured harness reports
# its safety properties green. Gate them on having observed something at all.
EVIDENCE_COUNT=$(echo "$CALLS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
echo "== enforcement records observed this run: $EVIDENCE_COUNT"

# Verdicts are NOT visible in the stub -- asserting only on the stub's call log
# is what let a silent drop pass as success here: "no enforcement call" was read
# as "routed to review" when in fact NO rule matched and nothing happened at all.
# Every case now asserts a positive outcome.
#
# The source of truth is the worker's own output_sink line (structured JSON,
# always emitted under OSPREY_STDOUT_OUTPUT_SINK=True), not ClickHouse: the
# ClickHouse sink batches (OSPREY_CLICKHOUSE_BATCH_SIZE) so a short run may
# never flush, which would make an assertion pass or fail for the wrong reason.
echo
echo "== verdicts as recorded by the worker"
LOGS=$(docker logs divine-worker --since 5m 2>&1)

verdict_for() {
  # Match the whole result line, not a fixed window: the number of fields
  # before __verdicts varies by which effects fired.
  # `|| true` matters: no match is a legitimate outcome (an event that
  # produced no verdict), and under `set -e` a bare assignment from a failing
  # grep aborts the script instead of failing the assertion -- a failure that
  # does not report as one, which is the bug class this harness exists to catch.
  { echo "$LOGS" | grep "\"__action_id\": $1," | head -1 \
    | grep -o '"__verdicts": \[[^]]*\]' | head -1; } || true
}
rules_for() {
  { echo "$LOGS" | grep "\"__action_id\": $1," | head -1 \
    | grep -o "RuleT(name='[^']*'" | sed "s/RuleT(name='//;s/'//" | tr '\n' ' '; } || true
}

for id in $A1 $A2 $A3 $A4 $A5; do
  echo "  action $id: $(verdict_for $id)  rules: $(rules_for $id)"
done

fail=0
check() {  # check <label> <condition-result>
  if [ "$2" = "1" ]; then echo "  PASS  $1"; else echo "  FAIL  $1"; fail=1; fi
}

echo
echo "== assertions"

# case 1: enforced, and the call carries the right hash and action
c1=$(echo "$CALLS" | python3 -c "
import json,sys
calls=json.load(sys.stdin)
def enforced(h):
    return any(c['path']=='/api/moderate-media'
               and c['body'].get('sha256')==h
               and c['body'].get('action')=='AGE_RESTRICTED' for c in calls)
print(1 if enforced('$MEDIA_HASH') and enforced('$MEDIA_HASH_4') else 0)")
check "cases 1 AND 4 each produced an AGE_RESTRICTED enforcement" "$c1"
check "case 1 verdict is restrict" \
  "$([ "$(verdict_for $A1)" != "" ] && echo "$(verdict_for $A1)" | grep -q restrict && echo 1 || echo 0)"

# case 2: the regression this harness previously missed. Absence of an
# enforcement call is NOT sufficient -- require the review verdict.
v2=$(verdict_for $A2)
check "case 2 produced a verdict at all (not silently dropped)" \
  "$([ -n "$v2" ] && echo 1 || echo 0)"
check "case 2 verdict is flag_for_review" \
  "$(echo "$v2" | grep -q flag_for_review && echo 1 || echo 0)"

# case 3: untrusted signer must produce no enforcement AND no verdict.
c3=$(echo "$CALLS" | python3 -c "
import json,sys
calls=json.load(sys.stdin)
print(0 if any('$CLAIMED_PUBKEY' in json.dumps(c) for c in calls) else 1)")
check "case 3 (untrusted signer) triggered no enforcement call" \
  "$([ "$EVIDENCE_COUNT" -gt 0 ] && echo "$c3" || echo 0)"
check "case 3 produced no enforcement verdict" \
  "$([ -z "$(verdict_for $A3 | grep -E 'restrict|ban')" ] && echo 1 || echo 0)"

# case 4: enforcement without a target event -- the majority shape.
v4=$(verdict_for $A4)
check "case 4 (hash, no target) verdict is restrict" \
  "$(echo "$v4" | grep -q restrict && echo 1 || echo 0)"

# case 5: malformed hash must produce review AND no enforcement.
v5=$(verdict_for $A5)
check "case 5 (malformed hash) verdict is flag_for_review" \
  "$(echo "$v5" | grep -q flag_for_review && echo 1 || echo 0)"
check "case 5 declared NO restrict verdict (no phantom enforcement)" \
  "$(echo "$v5" | grep -q restrict && echo 0 || echo 1)"
# Both of these are gated on EVIDENCE_COUNT: see the note above. Without the
# gate they are satisfied by an empty log, which is the failure mode this
# harness was in before 2026-08-11.
check "no enforcement call carried the malformed hash" \
  "$([ "$EVIDENCE_COUNT" -gt 0 ] && ! echo "$CALLS" | grep -q 'not-a-valid-sha256' && echo 1 || echo 0)"

check "no call ever carried the claimed (unresolved) pubkey" \
  "$([ "$EVIDENCE_COUNT" -gt 0 ] && ! echo "$CALLS" | grep -q "$CLAIMED_PUBKEY" && echo 1 || echo 0)"

echo
if [ "$fail" = "0" ]; then
  echo "ALL ASSERTIONS PASSED"
else
  echo "FAILURES ABOVE"; exit 1
fi
