import os
from typing import Any, Optional

import requests
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase
from osprey.worker.lib.osprey_shared.logging import get_logger
from reported_author import normalize_event_id, resolve_author, shared_cache

logger = get_logger(__name__)

_TIMEOUT = 1.5


class ResolveEventAuthorArguments(ArgumentsBase):
    event_id: str
    """The id of the reported event, from the report's `e` tag."""


class ResolveEventAuthor(UDFBase[ResolveEventAuthorArguments, str]):
    """Resolves who actually signed a reported event, or '' if it cannot be trusted.

    A report's `p` tag names the account being reported, but the reporter writes
    it and nothing checks it against the event's real author. Rules that enforce
    on `ReportedPubkey` are therefore acting on unverified input. This resolves
    the author from the reported event itself.

    Returns '' on any failure, including a not-found event, an unreachable
    relay API, or a response whose event id does not match the request.
    Callers must treat '' as "no authoritative author" and decline to enforce.
    See `reported_author` for the guarantees and why failures are not cached.

    Reads funnelcake's ``GET /api/event/{id}``, which serves the event straight
    from ClickHouse. That matters: it gives a definitive found or not-found,
    rather than the "no events, but we may have been cut off" ambiguity you get
    from a relay subscription that times out before EOSE.

    Verified against the live API 2026-07-30: the body is a bare Nostr event
    (``id``, ``pubkey``, ``created_at``, ``kind``, ``tags``, ``content``,
    ``sig``), a missing event is a 404, and no authentication is required.

    Configuration (environment variable):
      - ``DIVINE_RELAY_API_URL``: Base URL of the relay's HTTP API
        (default: ``https://relay.divine.video``).
    """

    def execute(self, execution_context: ExecutionContext, arguments: ResolveEventAuthorArguments) -> str:
        author = resolve_author(arguments.event_id, self._fetch, cache=shared_cache())

        if arguments.event_id and not author:
            # Failing closed is correct, but it must not be silent: downstream
            # this becomes an item with no creator, and without this line the
            # only signal is the adapter refusing an enforcement much later.
            #
            # The id comes from the report's e-tag and is attacker-controlled, so
            # only the validated form is logged, in full and never truncated. A
            # malformed value is reported as malformed rather than echoed, which
            # would let arbitrary text (newlines included) into the logs.
            event_id = normalize_event_id(arguments.event_id)
            if event_id:
                logger.warning('Could not resolve an author for reported event %s; declining to enforce', event_id)
            else:
                logger.warning('Report carried a malformed event id; declining to enforce')

        return author

    def _fetch(self, event_id: str) -> Optional[Any]:
        base_url = os.environ.get('DIVINE_RELAY_API_URL', 'https://relay.divine.video').rstrip('/')

        resp = requests.get(
            f'{base_url}/api/event/{event_id}',
            headers={'Accept': 'application/json'},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        # Any other non-2xx raises, and resolve_author turns that into '' so we
        # decline to enforce rather than guessing.
        resp.raise_for_status()
        return resp.json()
