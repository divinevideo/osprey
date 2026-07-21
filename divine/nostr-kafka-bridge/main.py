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

# Bounded, kind-scoped subscription. An unbounded REQ ({}) makes the relay sort
# its entire stored history on every (re)connect, which trips ClickHouse's
# per-user memory cap on staging (Code: 241 MEMORY_LIMIT_EXCEEDED on
# relay_event_list), returns zero, and drops the sub on the WS keepalive timeout
# -- silently starving Osprey ingestion. An all-kinds query stays too big even
# with since+limit (still zero); scoping to the kinds the rules act on keeps each
# query cheap. Live events still stream after EOSE. All env-tunable without a
# rebuild. Context: iac#1230 (staging relay ClickHouse sizing).
SUBSCRIBE_LOOKBACK_SECONDS = int(os.environ.get('SUBSCRIBE_LOOKBACK_SECONDS', '3600'))
SUBSCRIBE_LIMIT = int(os.environ.get('SUBSCRIBE_LIMIT', '500'))


def _parse_kinds(env_val: str, default: list) -> list:
    """Parse a comma-separated kinds env override, else the default list.

    Unset (empty/whitespace) falls back to the default. A set-but-malformed
    override (e.g. ',' or garbage) must NOT silently parse to [] -- an empty
    kinds filter matches no events and would stop ingestion, so it's a hard
    configuration error.
    """
    if not env_val.strip():
        return default
    kinds = [int(k) for k in env_val.split(',') if k.strip()]
    if not kinds:
        raise ValueError(
            f'kinds override {env_val!r} parsed to no kinds; refusing to '
            'subscribe to an empty kind set (matches nothing / stops ingestion)'
        )
    return kinds


# Kinds Osprey acts on. Moderation kinds (1984 reports, 1985 labels -- the COOP
# path) get their own filter so a burst of high-volume content can't crowd them
# out of the capped replay. Content kinds feed the behavioral/video models.
MODERATION_KINDS = _parse_kinds(os.environ.get('SUBSCRIBE_MODERATION_KINDS', ''), [1984, 1985])
CONTENT_KINDS = _parse_kinds(os.environ.get('SUBSCRIBE_CONTENT_KINDS', ''), [0, 1, 1111, 34235, 34236])

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


def build_subscription_filters(now_ts: int, high_water: int | None = None) -> list:
    """Build bounded, kind-scoped REQ filters (recent window + capped replay).

    Never an unbounded {} filter and never all-kinds: both make the staging
    relay sort a set too large for ClickHouse's per-user memory limit, which
    returns zero events (see iac#1230). Moderation and content kinds are separate
    filters so the capped replay can't starve reports/labels. Live events still
    stream after EOSE, so ongoing ingestion is unaffected by the bound. Set
    SUBSCRIBE_LOOKBACK_SECONDS=0 to fall back to a limit-only bound.

    Reconnect cursor: `high_water` is the newest created_at already published to
    Kafka. On reconnect we resume from it (so events aren't replayed with fresh
    action IDs) but never earlier than the cold-start floor (now - lookback),
    which bounds backfill after a long outage so the query stays servable. Since
    is clamped to now: created_at is attacker-controlled, and a future-dated
    event must not poison the cursor and stall ingestion.
    """
    floor = now_ts - SUBSCRIBE_LOOKBACK_SECONDS if SUBSCRIBE_LOOKBACK_SECONDS > 0 else None
    candidates = [t for t in (floor, high_water) if t is not None]
    since = max(candidates) if candidates else None
    if since is not None:
        since = min(since, now_ts)
    base: dict = {'limit': SUBSCRIBE_LIMIT}
    if since is not None:
        base['since'] = since
    return [
        {**base, 'kinds': MODERATION_KINDS},
        {**base, 'kinds': CONTENT_KINDS},
    ]


# --- Main loop ---
async def bridge():
    global connected
    producer = make_producer()
    backoff = 1
    event_count = 0
    # Newest created_at published to Kafka, kept across reconnects so we resume
    # from where we left off instead of replaying the whole window each time.
    high_water: int | None = None

    while True:
        try:
            sub_id = uuid.uuid4().hex[:16]
            log.info('Connecting to %s (sub %s)', RELAY_URL, sub_id)

            async with websockets.connect(RELAY_URL) as ws:
                # Bounded, kind-scoped subscription (recent window + capped
                # replay); live events stream after EOSE. An unbounded {} or
                # all-kinds filter OOMs the relay's ClickHouse and starves
                # ingestion (returns zero) -- see iac#1230. On reconnect we
                # resume from high_water (the cursor) rather than re-fetching
                # the whole window.
                now_ts = int(datetime.now(timezone.utc).timestamp())
                filters = build_subscription_filters(now_ts, high_water)
                await ws.send(json.dumps(['REQ', sub_id, *filters]))
                connected = True
                backoff = 1
                log.info('Connected and subscribed (filters=%s)', filters)

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
                        # Advance the reconnect cursor to the newest event seen.
                        created = event.get('created_at')
                        if isinstance(created, int) and (high_water is None or created > high_water):
                            high_water = created
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
