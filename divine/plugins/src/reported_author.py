"""Authoritative resolution of the author of a reported Nostr event.

A kind-1984 report carries a `p` tag naming the account being reported, but that
tag is written by the *reporter* and is never checked against the reported
event's real author. The bridge copies it to `reported_pubkey`, which becomes
Osprey's `ReportedPubkey` and, downstream, Coop's `creator`. Anything that
enforces on that value is acting on unverified, reporter-controlled input.

Two ways that goes wrong. Adversarially, someone p-tags an account they want
acted on. Honestly and more commonly, a report carries no `p` tag at all, and
the value falls back to the report's own signer, so the *reporter* becomes the
target.

This module resolves the author from the reported event itself. The event id
comes from the report's `e` tag, and while an attacker chooses which event to
report, they cannot change who signed it.

Two properties matter and are tested:

- **The response is bound to the request.** The returned event's id must equal
  the id we asked for, so the relay cannot answer with a *different stored
  event* and thereby choose the pubkey we enforce against. Note the limit: the
  id is not recomputed from the body and the signature is not checked, so a
  compromised relay could still fabricate a response carrying the requested id
  and any pubkey. Closing that needs canonical NIP-01 id recomputation and a
  Schnorr dependency, and belongs with a decision about whether the relay sits
  inside our trust boundary at all.
- **It fails closed.** Any failure yields an empty string, never a guess.
  Callers must treat that as "no authoritative author" and decline to enforce,
  rather than falling back to the claimed value.

Failures are cached briefly, and the honest limit of that is worth stating:
**it does not stop an attacker driving outbound requests.** Reports e-tagging
distinct random 64-char hex ids are all guaranteed misses, so they still cost
one request each; caching only collapses repeats of the *same* id, which is not
what an attacker would send. The short negative TTL is there so one bad reply
does not pin an empty answer for the full positive TTL, and so eviction has
something cheap to discard first. Bounding the outbound rate needs a cap or a
circuit breaker, which is not implemented here.

This module imports nothing from Osprey so it can be unit tested without the
engine installed.
"""

import time
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple

# Nostr event ids and pubkeys are both 32 bytes, rendered as 64 hex chars, which
# is the same shape as a media sha256, so the pattern is shared rather than
# copied. It used to be a local `^[0-9a-f]{64}$`, and the two copies did not
# agree: in Python `$` also matches immediately before a trailing newline, so
# that form accepts "<64 hex>\n" while the shared `\A...\Z` does not.
#
# It was not reachable here, because `_hex64` strips before matching, but a
# duplicate that differs from its original in exactly the way the original
# documents avoiding is a trap left for whoever removes the strip.
from media_hash import HEX64_RE

CACHE_TTL_SECONDS = 300.0
# Failures are cached far more briefly than successes: long enough to blunt an
# attacker-driven miss storm, short enough that a transient outage does not
# suppress enforcement for minutes.
NEGATIVE_CACHE_TTL_SECONDS = 30.0
CACHE_MAX_SIZE = 1000


def _hex64(value: Any) -> str:
    """Normalize a 64-char hex string, or return '' if it is not one."""
    if not isinstance(value, str):
        return ''
    candidate = value.strip().lower()
    return candidate if HEX64_RE.match(candidate) else ''


def normalize_event_id(value: Any) -> str:
    """Public form of the id check, for callers that must not echo raw input.

    An event id arrives from a report's `e` tag and is attacker-controlled, so
    it must never reach a log or a URL unvalidated.
    """
    return _hex64(value)


def extract_author(payload: Any, requested_event_id: str) -> str:
    """Return the signer of `payload`, but only if it is the event we asked for.

    Binds the response to the request: a relay cannot answer with a *different
    stored event* and thereby choose the pubkey we enforce against. It does not
    verify the body, so a compromised relay could fabricate one carrying the
    requested id. See the module docstring for why that is left open.
    """
    if not isinstance(payload, dict):
        return ''

    returned_id = _hex64(payload.get('id'))
    wanted = _hex64(requested_event_id)
    if not returned_id or not wanted or returned_id != wanted:
        return ''

    return _hex64(payload.get('pubkey'))


def resolve_author(
    event_id: str,
    fetch: Callable[[str], Any],
    cache: MutableMapping[str, Tuple[str, float]],
    now: Optional[Callable[[], float]] = None,
    ttl: float = CACHE_TTL_SECONDS,
    negative_ttl: float = NEGATIVE_CACHE_TTL_SECONDS,
    max_size: int = CACHE_MAX_SIZE,
) -> str:
    """Resolve the author of `event_id`, returning '' when it cannot be trusted.

    `fetch` takes an event id and returns the event dict, or None if not found.
    """
    wanted = _hex64(event_id)
    if not wanted:
        # Never hand an unvalidated id to the fetcher: it is interpolated into a
        # URL path and written to logs.
        return ''

    clock = now or time.monotonic
    current = clock()

    cached = cache.get(wanted)
    if cached and cached[1] > current:
        return cached[0]

    try:
        payload = fetch(wanted)
    except Exception:
        _store(cache, wanted, '', current + negative_ttl, max_size, current)
        return ''

    author = extract_author(payload, wanted)
    if not author:
        # Cached, but only briefly. Not caching at all is an amplification
        # vector: an attacker publishes reports e-tagging random 64-char hex
        # ids, every one a guaranteed miss, and each becomes an uncached
        # outbound request whose rate they control. A short negative TTL caps
        # the repeats while still letting a transient failure retry soon,
        # rather than pinning an empty answer for the full positive TTL.
        _store(cache, wanted, '', current + negative_ttl, max_size, current)
        return ''

    # `current` is reused rather than re-reading the clock, so one call consumes
    # exactly one tick. That keeps an injected test clock honest.
    _store(cache, wanted, author, current + ttl, max_size, current)
    return author


def _store(
    cache: MutableMapping[str, Tuple[str, float]],
    key: str,
    author: str,
    expires_at: float,
    max_size: int,
    now: float,
) -> None:
    if len(cache) >= max_size:
        _evict(cache, max_size, now)
    cache[key] = (author, expires_at)


def _evict(cache: MutableMapping[str, Tuple[str, float]], max_size: int, now: float) -> None:
    """Make room, sacrificing the least valuable entries first.

    Order matters. Negative entries are evicted ahead of positive ones because
    an attacker controls how many of them exist: reports e-tagging distinct
    random ids are all guaranteed misses, and if negatives could evict positives
    a storm would flush real answers and force honest traffic to re-fetch,
    raising load on the relay API rather than lowering it. Evicting negatives
    first means a storm can only ever cost the attacker's own entries.
    """
    for key in [k for k, (_, expires_at) in cache.items() if expires_at <= now]:
        del cache[key]

    if len(cache) >= max_size:
        for key in [k for k, (author, _) in cache.items() if not author]:
            del cache[key]

    if len(cache) >= max_size:
        for key in list(cache.keys())[: max(1, len(cache) // 2)]:
            del cache[key]


REPORT_KIND = 1984
LABEL_KIND = 1985

# Features that only ever appear on a wrapper. Used as a backstop when Kind is
# missing or unrecognised.
# Deliberately excludes LabelSignerPubkey: it reads $.pubkey and so is populated
# on EVERY event, which would make every ordinary note look wrapped and stop
# resolving authors for real content.
_WRAPPER_MARKERS = (
    'LabelTargetEvent',
    'LabelTargetPubkey',
    'LabelContentHash',
    'LabelNamespace',
    'LabelValue',
    'ReportedEventId',
    'ReportedEvent',
    'ReportedPubkey',
    'ReportReason',
)


def _kind(value: Any) -> Optional[int]:
    """Coerce Kind to an int, or None if it is absent or not a whole number.

    No bool special-case: `isinstance(True, int)` is already True and a bool
    compares equal to 1, which is not a wrapper kind, so it falls through to the
    marker backstop exactly as an unrecognised kind should.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        # isascii() as well as isdecimal(): str.isdigit() is True for '²' and
        # friends, which int() then rejects with ValueError, and that raise
        # escaped into COOPSink above its own try block.
        if candidate.isascii() and candidate.isdecimal():
            return int(candidate)
    return None


def author_for_features(features: Any) -> str:
    """Return whoever signed the content being acted on, or '' if unresolvable.

    Reports and labels WRAP a target event. On those, the wrapper's own `Pubkey`
    is the reporter or our own moderation identity, not the offender, so it must
    never become an enforcement target. Direct content events are not wrapped,
    and there `Pubkey` IS the author.

    **This keys on `Kind`, deliberately, and not on which optional features
    happen to be populated.** Presence checks are unsound, because a wrapper can
    legitimately carry none of its target features:

    - A hash-only CSAM label has `LabelTargetEvent = None` by construction
      (`ConfirmedCSAMHashOnlyNullTarget` is a live rule emitting an actionable
      verdict), so a presence check misses it and returns our own moderation
      identity.
    - NIP-56 permits a report with only a p-tag, or with neither tag, so a
      report can carry no target features at all and return the reporter.

    Both put the wrong account into COOP's `creator`, which is the target of
    Unban-User and Unsuspend-User today, and of the purging `banpubkey` once
    s-t-s#190 moves forward actions onto `creator`.

    Returns '' whenever a wrapper's target cannot be resolved. Callers must
    treat that as "no authoritative author" and decline to enforce.
    """
    if not isinstance(features, dict):
        return ''

    kind = _kind(features.get('Kind'))

    if kind == LABEL_KIND:
        return str(features.get('LabelTargetAuthorPubkey') or '')

    if kind == REPORT_KIND:
        return str(features.get('ReportedAuthorPubkey') or '')

    # Kind absent or unrecognised. If it nonetheless looks wrapped, refuse
    # rather than fall back to the wrapper's signer.
    if any(features.get(marker) for marker in _WRAPPER_MARKERS):
        return ''

    return str(features.get('Pubkey') or '')


# Shared across UDF instances: Osprey builds one instance per call site at rule
# compile time, so a module-level cache is what makes the hit rate real.
_CACHE: Dict[str, Tuple[str, float]] = {}


def shared_cache() -> Dict[str, Tuple[str, float]]:
    return _CACHE


def content_id_for_features(features: Any) -> Optional[str]:
    """Return the id of the content being moderated, or None if there is none.

    Reports and labels wrap a target event, and it is the target that should be
    keyed on, not the wrapper. Keyed on `Kind` for the same reason as
    `author_for_features`, and crucially **so the two stay in step**: the author
    returned there is the author *of* the id returned here. Resolving them by
    different means allowed a crafted action to produce a contentId and a userId
    describing two different events.
    """
    if not isinstance(features, dict):
        return None

    kind = _kind(features.get('Kind'))

    if kind == LABEL_KIND:
        target = features.get('LabelTargetEvent')
    elif kind == REPORT_KIND:
        target = features.get('ReportedEventId')
    else:
        target = None

    for value in (target, features.get('EventId')):
        if value:
            return str(value)

    # Direct detector Actions have no Nostr event id yet. Their content hash is
    # computed from the bytes the detector fetched and is therefore the stable
    # review identity. Validate it before it can become a COOP contentId.
    detector_hash = _hex64(features.get('DetectorContentHash'))
    if detector_hash:
        return detector_hash
    return None
