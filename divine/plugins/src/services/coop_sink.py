import os
from typing import Any

import gevent
import requests
import sentry_sdk
from coop_profile import profile_fields
from funnelcake_profile import fetch_profile
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.engine.language_types.verdicts import VerdictEffect
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from reported_author import _hex64, author_for_features, content_id_for_features

from .nostr_media import fetch_event_media

logger = get_logger(__name__)

ACTIONABLE_VERDICTS = {'flag_for_review', 'restrict', 'auto_hide', 'ban'}
VERDICT_SEVERITY = {'flag_for_review': 0, 'restrict': 1, 'auto_hide': 2, 'ban': 3}


class COOPSink(BaseOutputSink):
    """Output sink that submits Osprey verdicts to COOP for human review.

    Sends flagged content to COOP's content submission API so moderators
    can review Osprey rule triggers in the Manual Review Tool (MRT).

    Configuration (environment variables):
      - ``DIVINE_COOP_URL``: COOP server URL
        (e.g. ``https://coop.staging.dvines.org``). Required.
      - ``DIVINE_COOP_API_KEY``: Organization API key for COOP. Required.
      - ``DIVINE_COOP_CONTENT_TYPE``: Content type name configured in COOP
        (default: ``nostr_event``).
      - ``DIVINE_RELAY_WS_URL``: relay websocket URL used to fetch the reported
        event's media so the MRT can show the video under review
        (e.g. ``wss://relay.staging.divine.video``). Optional; if unset, items are
        submitted without media (fail-open).
      - ``DIVINE_MEDIA_BASE_URL``: trusted base used to construct media URLs
        for content-hash detector Actions (default: ``https://media.divine.video``).
    """

    # Budget for the COOP submission itself.
    coop_timeout: float = 5.0
    # Short, fail-open bound on the relay media lookup so a slow relay never backs up
    # the sink; on timeout the item is submitted without media.
    media_timeout: float = 3.0
    # Wall-clock budget for one push(), enforced by MultiOutputSink's gevent.Timeout.
    # Derived rather than hardcoded because it has to cover both hops: share one budget
    # between them and a slow relay eats the POST's part, gevent kills the greenlet, and
    # the item is dropped (max_retries is 0) instead of merely submitted without media.
    # ONE budget for ALL profile lookups combined, not one each. Per-lookup would make
    # the worst case scale with the number of distinct pubkeys on an item, which is
    # exactly what the derived total below cannot absorb.
    profile_timeout: float = 3.0
    timeout: float = media_timeout + profile_timeout + coop_timeout + 1.0

    def __init__(self) -> None:
        self._url = os.environ.get('DIVINE_COOP_URL', '')
        self._api_key = os.environ.get('DIVINE_COOP_API_KEY', '')
        self._content_type = os.environ.get('DIVINE_COOP_CONTENT_TYPE', 'nostr_event')
        self._relay_ws_url = os.environ.get('DIVINE_RELAY_WS_URL', '')
        self._media_base_url = os.environ.get('DIVINE_MEDIA_BASE_URL', 'https://media.divine.video').rstrip('/')
        # Reuses the variable resolve_event_author already reads, rather than adding a
        # second name for the same funnelcake API. Unset disables profile enrichment,
        # exactly as an unset DIVINE_RELAY_WS_URL disables media.
        self._relay_api_url = os.environ.get('DIVINE_RELAY_API_URL', '').rstrip('/')

        if not self._url:
            logger.info('DIVINE_COOP_URL not set. COOPSink disabled.')
        else:
            if not self._api_key:
                logger.warning('DIVINE_COOP_API_KEY not set. COOPSink will fail auth.')
            if not self._relay_ws_url:
                logger.info('DIVINE_RELAY_WS_URL not set. COOP items will omit media (no MRT video).')

    def _headers(self) -> dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'x-api-key': self._api_key,
        }

    def will_do_work(self, result: ExecutionResult) -> bool:
        if not self._url or not self._api_key:
            return False
        return any(v.verdict.lower() in ACTIONABLE_VERDICTS for v in result.verdicts)

    def push(self, result: ExecutionResult) -> None:
        content_id = self._resolve_content_id(result.extracted_features)
        if content_id is None:
            logger.warning(
                'COOPSink: no resolvable content ID for action_id=%s, skipping',
                result.action.action_id,
            )
            return

        best_verdict: VerdictEffect | None = None
        for verdict in result.verdicts:
            key = verdict.verdict.lower()
            if key not in ACTIONABLE_VERDICTS:
                continue
            if best_verdict is None or VERDICT_SEVERITY.get(key, 0) > VERDICT_SEVERITY.get(
                best_verdict.verdict.lower(), 0
            ):
                best_verdict = verdict

        if best_verdict is not None:
            self._submit_content(content_id, best_verdict, result)

    def _resolve_content_id(self, features: dict[str, Any]) -> str | None:
        """Resolve the moderated content's id, not the wrapper event's.

        Delegates so that it is keyed on Kind exactly as `author_for_features`
        is, which keeps `contentId` and `userId` describing the same event by
        construction rather than by two rules that can drift apart.
        """
        return content_id_for_features(features)

    def _submit_content(self, content_id: str, verdict: VerdictEffect, result: ExecutionResult) -> None:
        features = result.extracted_features
        wrapper_event_id = features.get('EventId', str(result.action.action_id))

        # Whoever signed the content being moderated. Becomes COOP's `creator`,
        # which drives reversals (Unban-User / Unsuspend-User), so it must name
        # the offender and not the reporter or labeler who signed the wrapper.
        # '' when it cannot be resolved: COOP then carries no creator and the
        # enforcement adapter refuses loudly instead of acting on a guess.
        author = author_for_features(features)

        content: dict[str, Any] = {
            'event_id': content_id,
            'source_event_id': wrapper_event_id,
            # Describes event_id, not source_event_id. The wrapper's own signer
            # is not carried here; `reported_pubkey` below keeps the reporter's
            # unverified claim, clearly labelled as a claim.
            'pubkey': author,
            'kind': features.get('Kind'),
            'created_at': features.get('CreatedAt'),
            'verdict': verdict.verdict,
            'action_name': result.action.action_name,
        }

        if features.get('ReportReason'):
            content['report_reason'] = features['ReportReason']
        if features.get('ReportedPubkey'):
            content['reported_pubkey'] = features['ReportedPubkey']
        if features.get('ReportedEventId'):
            content['reported_event_id'] = str(features['ReportedEventId'])
        if features.get('LabelValue'):
            content['label_value'] = features['LabelValue']
        if features.get('LabelNamespace'):
            content['label_namespace'] = features['LabelNamespace']
        if features.get('NoteText'):
            content['text'] = features['NoteText']

        # Detector Actions are keyed on the SHA-256 computed from fetched
        # bytes, not on a Nostr event id. Build the playable URL from that
        # validated identity and a trusted base; never pass through the
        # caller-controlled URL carried for diagnostics in the Action.
        if result.action.action_name == 'ai_detector_nsfw' and features.get('DetectorContentHash') == content_id:
            content['media_url'] = f'{self._media_base_url}/{content_id}'
            content['label_namespace'] = 'content-warning'
            content['label_value'] = features.get('DetectorClass', 'nsfw')
            content['confidence'] = features.get('DetectorConfidence', 0)
            content['model'] = features.get('DetectorModel', '')

        # Resolve the reported content's playable media so the MRT can show the video
        # under review. content_id is the moderated event's id, but a report/label event
        # doesn't carry the media (it lives in that event's NIP-92 imeta tag), so fetch
        # it from the relay. Fail-open: no relay URL, or any fetch failure/timeout, just
        # means the item is submitted without media (never dropped, never blocked).
        if self._relay_ws_url and 'media_url' not in content:
            # Hard bound on the whole lookup, DNS and TLS handshake included, so it can
            # never reach into the COOP POST's share of the push budget above.
            media_deadline = gevent.Timeout(self.media_timeout)
            try:
                with media_deadline:
                    media_url, media_thumbnail = fetch_event_media(
                        self._relay_ws_url, content_id, timeout=self.media_timeout
                    )
                if media_url:
                    content['media_url'] = media_url
                if media_thumbnail:
                    content['media_thumbnail'] = media_thumbnail
            except gevent.Timeout as media_timeout_exc:
                if media_timeout_exc is not media_deadline:
                    raise  # the sink-level timeout; MultiOutputSink owns it
                logger.warning('COOP media lookup timed out for %s; submitting without media', content_id)
            except Exception:
                # Media is best-effort enrichment; never let it block a review submission.
                logger.exception('COOP media lookup failed for %s; submitting without media', content_id)

        # Put PEOPLE on the card, not 64 hex characters. Author, reported and reporter
        # each get their own namespaced fields so one person's identity is never shown
        # against another's actions.
        #
        # Deduplicated by pubkey: on a report the author and the reported user are
        # usually the same account, so this is one call in the common case rather than
        # three per item.
        #
        # Fail-open like the media lookup -- enrichment must never drop a review item.
        # But NOT silently: funnelcake answers 200 with a null profile for an unknown
        # pubkey, so a blank name would read as "this user has no profile" when it may
        # mean "we could not ask". profile_fields carries that distinction as a field.
        if self._relay_api_url:
            seen: dict[str, tuple[Any, Any]] = {}
            # One deadline around the WHOLE set. Per-lookup deadlines would let N
            # distinct pubkeys consume N * profile_timeout and eat the POST's share of
            # the push budget, which drops the item outright (max_retries is 0) rather
            # than submitting it unenriched. That is the failure this whole block is
            # supposed to be incapable of.
            profile_deadline = gevent.Timeout(self.profile_timeout)
            try:
                with profile_deadline:
                    for pubkey in (
                        author,
                        features.get('ReportedPubkey', ''),
                        features.get('Pubkey', ''),
                    ):
                        # Validate BEFORE the fetch: `ReportedPubkey` is the first p-tag
                        # of a report, i.e. reporter-controlled, and it is interpolated
                        # into a URL path. requests normalises dot segments, so an
                        # unvalidated value steers an in-cluster GET to an arbitrary
                        # funnelcake path and lands the URL in a moderator-visible error
                        # field. reported_author.py:118-120 already settled this.
                        if pubkey and _hex64(pubkey) and pubkey not in seen:
                            # Caught per pubkey, INSIDE the shared deadline. The deadline
                            # bounds total time; this keeps each failure's own reason,
                            # which is the difference between a card saying 'HTTP 500
                            # Unable To Extract Key!' -- the rate limiter, actionable --
                            # and a generic 'something went wrong'.
                            try:
                                seen[pubkey] = fetch_profile(self._relay_api_url, pubkey, timeout=self.profile_timeout)
                            except gevent.Timeout:
                                raise  # the shared deadline; handled below
                            except Exception as exc:  # noqa: BLE001
                                seen[pubkey] = (None, f'{type(exc).__name__}: {exc}')
            except gevent.Timeout as profile_timeout_exc:
                if profile_timeout_exc is not profile_deadline:
                    raise  # the sink-level timeout; MultiOutputSink owns it
                logger.warning('COOP profile lookups timed out; submitting with what resolved')
            except Exception:
                logger.exception('COOP profile lookup failed; submitting without profiles')

            for prefix, pubkey in (
                ('author', author),
                ('reported', features.get('ReportedPubkey', '')),
                ('reporter', features.get('Pubkey', '')),
            ):
                if not pubkey:
                    continue
                if not _hex64(pubkey):
                    content.update(profile_fields(None, prefix=prefix, error='not a 64-char hex pubkey'))
                    continue
                body, error = seen.get(pubkey, (None, 'lookup did not complete'))
                content.update(profile_fields(body, prefix=prefix, error=error))

        payload: dict[str, Any] = {
            'contentId': content_id,
            'contentType': self._content_type,
            'userId': author,
            'content': content,
            'sync': False,
        }

        try:
            resp = requests.post(
                f'{self._url}/api/v1/content',
                json=payload,
                headers=self._headers(),
                timeout=self.coop_timeout,
            )
            resp.raise_for_status()
            logger.info(
                'Submitted to COOP: content=%s source=%s verdict=%s kind=%s',
                content_id,
                wrapper_event_id,
                verdict.verdict,
                features.get('Kind'),
            )
        except Exception:
            logger.exception('Failed to submit to COOP: content=%s verdict=%s', content_id, verdict.verdict)
            sentry_sdk.capture_exception()

    def stop(self) -> None:
        pass
