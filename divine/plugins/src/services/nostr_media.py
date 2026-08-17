"""Resolve a nostr event's playable media URL and content hash from the relay for COOP review.

COOP's Manual Review Tool renders a VIDEO field only when the submitted item carries a
``media_url``. A kind-1984 report / kind-1985 label references the offending content by id
but does not carry its media, so COOPSink resolves the media by fetching the reported event
from the relay and reading its NIP-92 ``imeta`` tag (``url <playable>`` / ``thumb <image>``),
plus the same tag's ``x <sha256>`` field (top-level ``x`` tag as fallback) for the
restricted-media viewer link.

Everything here is FAIL-OPEN: any error, timeout, or missing field yields all-``None`` (a
``(None, None)`` pair, or a ``(None, None, None)`` triple on the with-hash path) so the COOP
submission proceeds without media rather than being dropped or blocked. The parse mirrors the
proven ``coop-bridge-import.sh`` reference.
"""

from __future__ import annotations

import json
import time
from typing import Any

from media_hash import HEX64_RE
from websocket import create_connection  # websocket-client (synchronous)

# One connection + one subscription per fetch, so a fixed subscription id is fine.
_SUB = 'coop-media'
# Secondary guard on the read loop. The wall-clock deadline in _fetch_raw_event is what
# actually bounds it; this just caps how many frames we are willing to parse to get there.
_MAX_FRAMES = 64
_DEFAULT_TIMEOUT = 3.0


def _media_triple_from_event(event: Any) -> tuple[str | None, str | None, str | None]:
    """Return ``(media_url, thumbnail_url, media_sha)`` from one event.

    When any imeta tag has a url, that SAME tag supplies the thumb and the sha, so
    all three describe one piece of media by construction. The sha from that imeta
    is never substituted from elsewhere: an event whose imeta names a playable url
    but carries no hash simply has no sha, because a top-level ``x`` tag on such an
    event could name other bytes (the reported event is authored by the person
    under moderation, and a card whose media_url plays A while its viewer link
    shows B is worse than a card with no link).

    The top-level ``x`` tag is read only in the NIP-94 shape, where NO imeta
    carries a url: there the event itself is file metadata keyed by the hash, so
    the tag is the only source and nothing can mismatch. Divine's publishers put
    the hash inside imeta (``x <sha>``) — see divine-mobile's
    video_event_publisher and this repo's label_routing.sml — so divine events
    never take the NIP-94 branch.

    Raw parse; no URL validation (the caller filters). Never raises; all-``None``
    for anything malformed, including a parse that failed partway through.
    """
    top_level_sha: str | None = None
    try:
        tags = event.get('tags') if isinstance(event, dict) else None
        if not isinstance(tags, list):
            return None, None, None
        for tag in tags:
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == 'x' and isinstance(tag[1], str):
                candidate = tag[1].strip().lower()
                if HEX64_RE.match(candidate):
                    top_level_sha = candidate
                    break
        for tag in tags:
            if not isinstance(tag, list) or len(tag) < 2 or tag[0] != 'imeta':
                continue
            url: str | None = None
            thumb: str | None = None
            imeta_sha: str | None = None
            for field in tag[1:]:
                if not isinstance(field, str):
                    continue
                if field.startswith('url '):
                    url = field[4:].strip()
                elif field.startswith('thumb '):
                    thumb = field[6:].strip()
                elif field.startswith('image ') and thumb is None:
                    thumb = field[6:].strip()
                elif field.startswith('x ') and imeta_sha is None:
                    candidate = field[2:].strip().lower()
                    if HEX64_RE.match(candidate):
                        imeta_sha = candidate
            if url:
                return url, thumb, imeta_sha
        return None, None, top_level_sha
    except Exception:
        pass
    return None, None, None


def media_from_event(event: Any) -> tuple[str | None, str | None]:
    """Return ``(media_url, thumbnail_url)`` from an event's first ``imeta`` tag with a url.

    Raw parse; no URL validation (the caller filters). Never raises; returns ``(None, None)``
    for anything malformed.
    """
    return _media_triple_from_event(event)[:2]


def media_hash_from_event(event: object) -> str | None:
    """Return the event's media sha256, or ``None``.

    Primary source is the ``x`` field of the imeta tag that also carries the
    media url (NIP-92); a top-level ``x`` tag is read only when no imeta carries
    a url (the NIP-94 file-metadata shape, where the tag is the event's own
    key). Used to build the Coop card's relay_manager_url media link.
    Fail-open: any malformed input yields ``None``. Normalised to lowercase so
    the link matches the proxy's key.
    """
    return _media_triple_from_event(event)[2]


def _is_http_url(value: str | None) -> bool:
    """A plain http(s) URL safe to hand to COOP (no control chars, bounded length)."""
    return (
        isinstance(value, str)
        and value.startswith(('https://', 'http://'))
        and len(value) <= 2048
        and '\r' not in value
        and '\n' not in value
    )


def _fetch_raw_event(relay_url: str, event_id: str, timeout: float = _DEFAULT_TIMEOUT) -> Any | None:
    """Fetch a single event by id from the relay and return its dict, or ``None``.

    Only an EVENT frame whose event id equals the requested ``event_id`` is
    accepted, so the relay cannot substitute a different stored event.

    Returns the raw, otherwise-unvalidated event JSON (``Any``): callers own
    field validation.

    Holds the whole fail-open contract: empty input, connect/read failure, timeout, or
    not-found all yield ``None``. Never raises. ``timeout`` is a wall-clock budget for the
    entire call, shared across reads, so a chatty relay cannot hold the caller open.
    """
    if not relay_url or not event_id:
        return None
    deadline = time.monotonic() + timeout
    ws = None
    try:
        ws = create_connection(relay_url, timeout=timeout)
        ws.send(json.dumps(['REQ', _SUB, {'ids': [event_id], 'limit': 1}]))
        for _ in range(_MAX_FRAMES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ws.settimeout(remaining)
            msg = json.loads(ws.recv())
            if not isinstance(msg, list) or len(msg) < 2 or msg[1] != _SUB:
                continue  # NOTICE / another subscription / junk frame
            if msg[0] == 'EVENT' and len(msg) >= 3:
                # Bind the answer to the request, as reported_author.extract_author
                # does: a relay answering with a different stored event must not
                # choose what the moderation card plays or links to.
                candidate = msg[2]
                if isinstance(candidate, dict) and candidate.get('id') == event_id:
                    return candidate
                continue
            if msg[0] in ('EOSE', 'CLOSED'):
                return None
    except Exception:
        return None
    finally:
        if ws is not None:
            try:
                # Send the close frame but don't wait for the peer's reply: against a stalled
                # relay websocket-client's default would hold us 3s past the deadline above.
                ws.close(timeout=0)
            except Exception:
                pass
    return None


def fetch_event_media(
    relay_url: str, event_id: str, timeout: float = _DEFAULT_TIMEOUT
) -> tuple[str | None, str | None]:
    """Fetch ``event_id`` and return its validated ``(media_url, thumb)``. Fail-open: ``(None, None)``."""
    event = _fetch_raw_event(relay_url, event_id, timeout)
    if event is None:
        return None, None
    # One parse for both fields; _is_http_url runs OUTSIDE _fetch_raw_event's try/except
    # and self-guards against malformed input, which is what keeps this wrapper's
    # fail-open contract intact.
    url, thumb, _ = _media_triple_from_event(event)
    return (url if _is_http_url(url) else None, thumb if _is_http_url(thumb) else None)


def fetch_event_media_with_hash(
    relay_url: str, event_id: str, timeout: float = _DEFAULT_TIMEOUT
) -> tuple[str | None, str | None, str | None]:
    """Like ``fetch_event_media`` but also returns the event's media sha256.

    One relay round-trip and one parse: url, thumb and sha come from the same
    imeta tag, so the viewer link names the exact media the card plays. Fail-open:
    ``(None, None, None)`` on any failure. The sha powers the Coop card's
    relay_manager_url link to the restricted-media viewer.
    """
    event = _fetch_raw_event(relay_url, event_id, timeout)
    if event is None:
        return None, None, None
    # _media_triple_from_event / _is_http_url all run OUTSIDE the fetch's
    # try/except; each self-guards, which is what keeps this wrapper's fail-open
    # contract intact.
    url, thumb, sha = _media_triple_from_event(event)
    return (
        url if _is_http_url(url) else None,
        thumb if _is_http_url(thumb) else None,
        sha,
    )
