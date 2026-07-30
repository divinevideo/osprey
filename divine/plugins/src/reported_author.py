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

- **The response is verified.** The returned event's id must equal the id we
  asked for. Without that check we would only have swapped a reporter-controlled
  pubkey for a relay-controlled one.
- **It fails closed.** Any failure yields an empty string, never a guess.
  Callers must treat that as "no authoritative author" and decline to enforce,
  rather than falling back to the claimed value.

Failures are deliberately not cached, so a transient relay problem retries
rather than pinning an empty answer for the whole TTL. Reports are low volume,
so the extra calls are affordable.

This module imports nothing from Osprey so it can be unit tested without the
engine installed.
"""

import re
import time
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple

# Nostr event ids and pubkeys are both 32 bytes, rendered as 64 hex chars.
# `check_moderation_result` keeps its own copy of this pattern; if divine#8
# lands, both should move to the shared `media_hash.HEX64_RE`.
HEX64_RE = re.compile(r'^[0-9a-f]{64}$', re.IGNORECASE)

CACHE_TTL_SECONDS = 300.0
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

    The id check is the security boundary: a relay (or anything between us and
    it) must not be able to answer with a different event and thereby choose the
    pubkey we enforce against.
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
        return ''

    author = extract_author(payload, wanted)
    if not author:
        # Not cached: a transient failure should retry rather than pin an empty
        # answer for the whole TTL.
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
    """Drop expired entries first, then oldest-inserted if still over budget."""
    for key in [k for k, (_, expires_at) in cache.items() if expires_at <= now]:
        del cache[key]

    if len(cache) >= max_size:
        for key in list(cache.keys())[: max(1, len(cache) // 2)]:
            del cache[key]


def author_for_features(features: Any) -> str:
    """Return whoever signed the content being acted on, or '' if unresolvable.

    Reports and labels WRAP a target event. On those, the wrapper's own `Pubkey`
    is the reporter or our own moderation identity, not the offender, so it must
    never be used as an enforcement target. Direct content events are not
    wrapped, and there `Pubkey` IS the author.

    The key order mirrors COOPSink's `_resolve_content_id`, so the returned
    author always belongs to the same event that becomes `contentId`.
    """
    if not isinstance(features, dict):
        return ''

    if features.get('LabelTargetEvent'):
        return str(features.get('LabelTargetAuthorPubkey') or '')

    # ReportedPubkey is checked as well as ReportedEventId because NIP-56 allows
    # a report with only a p-tag and no e-tag, e.g. a profile report. The bridge
    # sets reported_event_id only when an e-tag exists, so keying solely on it
    # would let a p-only report fall through to Pubkey, which on a report is the
    # reporter. There is no event to resolve an author from in that case, so the
    # honest answer is none.
    if features.get('ReportedEventId') or features.get('ReportedPubkey'):
        return str(features.get('ReportedAuthorPubkey') or '')

    return str(features.get('Pubkey') or '')


# Shared across UDF instances: Osprey builds one instance per call site at rule
# compile time, so a module-level cache is what makes the hit rate real.
_CACHE: Dict[str, Tuple[str, float]] = {}


def shared_cache() -> Dict[str, Tuple[str, float]]:
    return _CACHE
