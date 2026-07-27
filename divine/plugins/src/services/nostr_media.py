"""Resolve a nostr event's playable media URL from the relay for COOP review display.

COOP's Manual Review Tool renders a VIDEO field only when the submitted item carries a
``media_url``. A kind-1984 report / kind-1985 label references the offending content by id
but does not carry its media, so COOPSink resolves the media by fetching the reported event
from the relay and reading its NIP-92 ``imeta`` tag (``url <playable>`` / ``thumb <image>``).

Everything here is FAIL-OPEN: any error, timeout, or missing field yields ``(None, None)`` so
the COOP submission proceeds without media rather than being dropped or blocked. The parse
mirrors the proven ``coop-bridge-import.sh`` reference.
"""

from __future__ import annotations

import json
from typing import Any

from websocket import create_connection  # websocket-client (synchronous)

# One connection + one subscription per fetch, so a fixed subscription id is fine.
_SUB = 'coop-media'
# Bound the read loop so a relay that streams unrelated frames (never our EVENT/EOSE) can't
# wedge the sink: the socket timeout bounds a stalled recv, this bounds a chatty one.
_MAX_FRAMES = 64
_DEFAULT_TIMEOUT = 3.0


def media_from_event(event: Any) -> tuple[str | None, str | None]:
    """Return ``(media_url, thumbnail_url)`` from an event's first ``imeta`` tag with a url.

    Raw parse; no URL validation (the caller filters). Never raises; returns ``(None, None)``
    for anything malformed.
    """
    try:
        tags = event.get('tags') if isinstance(event, dict) else None
        if not isinstance(tags, list):
            return None, None
        for tag in tags:
            if not isinstance(tag, list) or len(tag) < 2 or tag[0] != 'imeta':
                continue
            url: str | None = None
            thumb: str | None = None
            for field in tag[1:]:
                if not isinstance(field, str):
                    continue
                if field.startswith('url '):
                    url = field[4:].strip()
                elif field.startswith('thumb '):
                    thumb = field[6:].strip()
                elif field.startswith('image ') and thumb is None:
                    thumb = field[6:].strip()
            if url:
                return url, thumb
    except Exception:
        pass
    return None, None


def _is_http_url(value: str | None) -> bool:
    """A plain http(s) URL safe to hand to COOP (no control chars, bounded length)."""
    return (
        isinstance(value, str)
        and value.startswith(('https://', 'http://'))
        and len(value) <= 2048
        and '\r' not in value
        and '\n' not in value
    )


def fetch_event_media(
    relay_url: str, event_id: str, timeout: float = _DEFAULT_TIMEOUT
) -> tuple[str | None, str | None]:
    """Fetch ``event_id`` from ``relay_url`` and return its validated ``(media_url, thumb)``.

    Fail-open: returns ``(None, None)`` on empty input, connect/read failure, timeout,
    not-found, or non-http(s) media so the caller submits the item without media. Never raises.
    """
    if not relay_url or not event_id:
        return None, None
    ws = None
    try:
        ws = create_connection(relay_url, timeout=timeout)
        ws.settimeout(timeout)
        ws.send(json.dumps(['REQ', _SUB, {'ids': [event_id], 'limit': 1}]))
        for _ in range(_MAX_FRAMES):
            msg = json.loads(ws.recv())
            if not isinstance(msg, list) or len(msg) < 2 or msg[1] != _SUB:
                continue  # NOTICE / another subscription / junk frame
            if msg[0] == 'EVENT' and len(msg) >= 3:
                url, thumb = media_from_event(msg[2])
                return (
                    url if _is_http_url(url) else None,
                    thumb if _is_http_url(thumb) else None,
                )
            if msg[0] in ('EOSE', 'CLOSED'):
                return None, None
    except Exception:
        return None, None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return None, None
