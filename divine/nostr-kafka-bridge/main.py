"""Nostr-Kafka Bridge: subscribes to a Nostr relay, wraps events in Osprey Action format,
and publishes to Kafka for the Osprey rules engine."""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import websockets
from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('nostr-kafka-bridge')

RELAY_URL = os.environ.get('RELAY_URL', 'wss://relay.divine.video')
KAFKA_BOOTSTRAP_SERVERS = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
KAFKA_TOPIC = os.environ.get('KAFKA_TOPIC', 'osprey.actions_input')
HEALTH_PORT = int(os.environ.get('HEALTH_PORT', '8080'))

connected = False
_action_counter = 0

# --- Report reason normalization ---
# Divine clients use different reason vocabularies. Normalize to canonical
# values that SML rules can match consistently.
#
# Canonical values and their downstream owners are defined in CANONICAL_REASONS below.
# Mobile maps csam -> 'illegal' and sexual content -> 'nudity' per NIP-56.
# Web passes raw reasons (csam, harassment, sexual-content, etc.). divine-web#364
# splits child safety into three distinct categories, after which divine-web will send
# the hyphenated tokens 'child-safety', 'csam', and 'underage-user' (alongside the
# existing 'sexual-content', 'ai-generated', 'false-info'); they are aliased ahead of
# that merge so routing is correct the moment web ships it. The hyphenated web forms
# are aliased to canonical below; divine-mobile's camelCase 'childSafety' /
# 'underageUser' collapse to 'childsafety' / 'underageuser'.
# NB: divine-web sends hyphenated lowercase reasons (e.g. 'sexual-content',
# 'ai-generated'); divine-mobile sends camelCase reason.name (e.g. 'sexualContent',
# 'aiGenerated', 'childSafety') inside an 'NS-' NIP-32 label. Both arrive here
# lowercased after _normalize_report_reason's strip().lower(), so mobile's
# camelCase collapses to a single token ('sexualcontent', 'childsafety', ...).
# We must alias BOTH spellings or the report falls through to General Review and
# misses the SML auto-hide/threshold rules.
#
# CANONICAL_REASONS is the SINGLE SOURCE OF TRUTH for the tokens this bridge emits
# and who owns each one downstream. Every _REASON_ALIASES value must be a key here,
# and nothing should emit a token that is not catalogued here. Ownership drives where
# a report is acted on:
#   'osprey-rule'   -- a divine/rules/rules/reports/*.sml rule matches
#                      ReportReason == <token> and emits an actionable verdict
#                      (auto-hide / flag-for-review / threshold).
#   'relay-manager' -- handled by the relay-manager ReportWatcher + Zendesk path,
#                      NOT Osprey (e.g. age review's 15-day clock). Osprey has no
#                      rule for it by design.
#   'default-queue' -- no dedicated handling; falls to COOP General Review for a
#                      human to triage.
# The coupling tests in test_main.py parse the live .sml rules and fail if an
# 'osprey-rule' token has no matching rule, if a rule references a token not
# catalogued here, or if an alias resolves to a non-canonical token -- so this
# table cannot silently drift from the rules.
CANONICAL_REASONS = {
    'csam': 'osprey-rule',  # auto_hide (immediate); NCMEC-bound
    'illegal': 'osprey-rule',  # mobile's CSAM/violence/copyright overload; auto_hide matches it
    'child_safety': 'osprey-rule',  # FirstChildSafetyReport -> human review queue
    'harassment': 'osprey-rule',  # FirstHarassmentReport -> human review queue
    'nudity': 'osprey-rule',  # first/threshold sexual-content rules
    'violence': 'osprey-rule',  # first/threshold violence rules
    'ai_generated': 'osprey-rule',  # moderation_service rule
    'underage_user': 'relay-manager',  # age review: ReportWatcher 15-day clock + Zendesk, not Osprey
    'spam': 'default-queue',
    'impersonation': 'default-queue',
    'other': 'default-queue',
}

_REASON_ALIASES = {
    # CSAM -- 'illegal' is mobile's overload for CSAM/violence/copyright, so we
    # keep it as-is for human triage. 'sexual_minors' and 'csam' are unambiguous.
    'sexual_minors': 'csam',
    'ns-csam': 'csam',
    # Child safety -- distinct from CSAM and from age-review. Routes to its own
    # "Child Safety" queue for human triage; a moderator escalates to csam if warranted.
    'child-safety': 'child_safety',  # divine-web#364 (hyphenated web token)
    'childsafety': 'child_safety',  # divine-mobile camelCase collapsed
    'ns-childsafety': 'child_safety',
    # Underage user (age review) -- feeds the relay-manager age-review case system
    # (15-day clock, age tiers, suspension). Routes to its own "Age Review" queue.
    'underage-user': 'underage_user',  # divine-web#364 (hyphenated web token)
    'underageuser': 'underage_user',  # divine-mobile camelCase collapsed
    'ns-underageuser': 'underage_user',
    # Nudity/sexual content (incl. divine-mobile 'sexualContent' -> 'sexualcontent')
    'sexual-content': 'nudity',
    'sexualcontent': 'nudity',
    'sexual': 'nudity',
    'explicit': 'nudity',
    'pornography': 'nudity',
    'ns-nudity': 'nudity',
    'ns-sexual-content': 'nudity',
    # Harassment
    'profanity': 'harassment',
    'ns-harassment': 'harassment',
    # Spam
    'ns-spam': 'spam',
    # Violence
    'ns-violence': 'violence',
    # AI-generated (divine-web 'ai-generated', divine-mobile 'aiGenerated')
    'ai-generated': 'ai_generated',
    'aigenerated': 'ai_generated',
    # Other (divine-web 'false-info', divine-mobile 'falseInformation')
    'false-information': 'other',
    'false-info': 'other',
    'falseinformation': 'other',
    'ns-other': 'other',
    # MOD namespace labels from moderation-service kind 1984 reports.
    # These are the raw l-tag values: NS (Not Safe), VI (Violence), AI (AI-generated).
    # The bridge receives them lowercased after strip().lower() in _normalize_report_reason.
    'ns': 'nudity',
    'vi': 'violence',
    'ai': 'ai_generated',
}


def _normalize_report_reason(raw: str) -> str:
    """Normalize report reason to canonical value for SML rule matching."""
    raw = raw.strip().lower()
    return _REASON_ALIASES.get(raw, raw)


# --- Health check ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200 if connected else 503
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'ok' if connected else b'disconnected')

    def log_message(self, *_):
        pass


def start_health_server():
    server = HTTPServer(('0.0.0.0', HEALTH_PORT), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    log.info('Health check listening on :%d', HEALTH_PORT)


# --- Kafka producer ---
def make_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS.split(','),
        value_serializer=lambda v: json.dumps(v).encode(),
    )


def _next_action_id() -> int:
    """Generate a unique numeric action ID.

    Osprey's Action dataclass requires action_id: int and the Kafka input
    stream parser calls int(action_id). Use lower 20 bits of timestamp
    plus a counter to stay within safe integer range without collisions
    across bridge restarts.
    """
    global _action_counter
    _action_counter += 1
    ts_part = int(datetime.now(timezone.utc).timestamp()) % (2**20)
    return ts_part * 100000 + (_action_counter % 100000)


def _wrap_nostr_event(event: dict) -> dict:
    """Wrap a raw Nostr event into the Osprey Action envelope format.

    Extracts tag data needed by SML models (reported_event_id, reported_pubkey,
    report_reason, mentioned_pubkeys) so rules can reference them via JsonData paths.
    """
    kind = event.get('kind', 0)
    created_at = event.get('created_at', 0)
    tags = event.get('tags', [])

    # Build the data payload starting with raw Nostr fields
    data = {
        'event_id': event.get('id', ''),
        'pubkey': event.get('pubkey', ''),
        'kind': kind,
        'created_at': created_at,
        'content': event.get('content', ''),
        'tags': tags,
    }

    # Extract mentioned pubkeys from p-tags (for kind 1 notes)
    p_tags = [t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == 'p']
    if p_tags:
        data['mentioned_pubkeys'] = p_tags

    # Extract video hash from x-tag (for kind 34235/34236 video events)
    if kind in (34235, 34236):
        for t in tags:
            if isinstance(t, list) and len(t) >= 2 and t[0] == 'x':
                data['video_hash'] = t[1]
                break

    # Extract label fields for kind 1985 (NIP-32 label events)
    if kind == 1985:
        for t in tags:
            if isinstance(t, list) and len(t) >= 2:
                if t[0] == 'L':
                    data['label_namespace'] = t[1]
                elif t[0] == 'l' and len(t) >= 3:
                    data['label_value'] = t[1]
                    # Parse metadata from 4th element if present
                    if len(t) >= 4:
                        try:
                            meta = json.loads(t[3])
                            data['label_metadata'] = t[3]
                            if isinstance(meta, dict):
                                if 'confidence' in meta:
                                    data['label_confidence'] = float(meta['confidence'])
                                if 'source' in meta:
                                    data['label_source'] = meta['source']
                                data['label_rejected'] = bool(meta.get('rejected', False))
                                if 'sha256' in meta:
                                    data['label_content_hash'] = meta['sha256']
                        except (json.JSONDecodeError, TypeError, ValueError):
                            data['label_metadata'] = t[3]
                elif t[0] == 'e':
                    data['label_target_event'] = t[1]
                elif t[0] == 'p':
                    data['label_target_pubkey'] = t[1]
                elif t[0] == 'x':
                    data['label_content_hash'] = t[1]

    # Extract report-specific fields for kind 1984 (NIP-56 moderation reports)
    #
    # Divine clients use different tag formats:
    #   Mobile: ['e', eventId, nip56Type], ['p', pubkey, nip56Type]
    #           reason in 3rd element of e/p tags (spam, nudity, illegal, profanity, other)
    #   Web:    ['e', eventId, reason], ['p', pubkey, reason]
    #           plus ['l', 'NS-reason', 'social.nos.ontology']
    #   Generic: ['report', reason] or ['l', reason, 'MOD']
    #
    # Normalize report reasons to canonical values for rule matching.
    if kind == 1984:
        e_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == 'e']
        if e_tags:
            data['reported_event_id'] = e_tags[0][1]
        if p_tags:
            data['reported_pubkey'] = p_tags[0]

        # Extract raw reason from multiple sources (priority order)
        raw_reason = None

        # 1. Explicit 'report' tag (generic format)
        for t in tags:
            if isinstance(t, list) and len(t) >= 2 and t[0] == 'report':
                raw_reason = t[1]
                break

        # 2. NIP-32 label with social.nos.ontology namespace (divine-web)
        if not raw_reason:
            for t in tags:
                if isinstance(t, list) and len(t) >= 3 and t[0] == 'l' and t[2] == 'social.nos.ontology':
                    # Strip 'NS-' prefix from divine-web labels
                    raw_reason = t[1].removeprefix('NS-') if t[1].startswith('NS-') else t[1]
                    break

        # 3. NIP-32 label with MOD namespace
        if not raw_reason:
            for t in tags:
                if isinstance(t, list) and len(t) >= 3 and t[0] == 'l' and t[2] == 'MOD':
                    raw_reason = t[1]
                    break

        # 4. 3rd element of e or p tags (divine-mobile and divine-web primary format)
        if not raw_reason:
            if e_tags and len(e_tags[0]) >= 3 and e_tags[0][2]:
                raw_reason = e_tags[0][2]
            elif p_tags and isinstance(p_tags[0], str):
                # p_tags[0] is already the pubkey string, check raw tag
                p_tag_raw = [t for t in tags if isinstance(t, list) and len(t) >= 3 and t[0] == 'p']
                if p_tag_raw:
                    raw_reason = p_tag_raw[0][2]

        # 5. Content JSON (moderation-service automated reports)
        if not raw_reason:
            try:
                content_json = json.loads(event.get('content', ''))
                if isinstance(content_json, dict) and 'type' in content_json:
                    raw_reason = content_json['type']
            except (json.JSONDecodeError, TypeError):
                pass

        # 6. Keyword scan in content text (last resort).
        # Only match if content is the keyword alone (not a substring of freetext).
        # 'illegal' excluded: ambiguous in freetext and would escalate to auto-hide
        # for trusted reporters via the CSAM rule.
        if not raw_reason:
            content_lower = event.get('content', '').strip().lower()
            for reason in (
                'csam',
                'sexual_minors',
                'child_safety',
                'nudity',
                'violence',
                'harassment',
                'spam',
                'impersonation',
            ):
                if content_lower == reason:
                    raw_reason = reason
                    break

        # Normalize reason to canonical values used by SML rules
        if raw_reason:
            data['report_reason'] = _normalize_report_reason(raw_reason)

    # ISO timestamp for the wrapper
    try:
        send_time = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        send_time = datetime.now(timezone.utc).isoformat()

    return {
        'send_time': send_time,
        'data': {
            'action_id': _next_action_id(),
            'action_name': f'nostr_kind_{kind}',
            'data': data,
        },
    }


# --- Main loop ---
async def bridge():
    global connected
    producer = make_producer()
    backoff = 1
    event_count = 0

    while True:
        try:
            sub_id = uuid.uuid4().hex[:16]
            log.info('Connecting to %s (sub %s)', RELAY_URL, sub_id)

            async with websockets.connect(RELAY_URL) as ws:
                # Subscribe to all events
                await ws.send(json.dumps(['REQ', sub_id, {}]))
                connected = True
                backoff = 1
                log.info('Connected and subscribed')

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(msg, list) or len(msg) < 3:
                        continue

                    if msg[0] == 'EVENT':
                        event = msg[2]
                        wrapped = _wrap_nostr_event(event)
                        producer.send(KAFKA_TOPIC, value=wrapped)
                        event_count += 1
                        if event_count % 100 == 1:
                            log.info(
                                'Published %d events (latest: kind %s id %s)',
                                event_count,
                                event.get('kind', '?'),
                                event.get('id', '?')[:12],
                            )
                        else:
                            log.debug('Published event %s', event.get('id', '?')[:12])

        except Exception as exc:
            connected = False
            log.warning('Disconnected (%s), retrying in %ds', exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    start_health_server()
    asyncio.run(bridge())


if __name__ == '__main__':
    main()
