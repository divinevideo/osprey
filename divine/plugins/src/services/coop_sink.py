import os
from typing import Any

import requests
import sentry_sdk
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.engine.language_types.verdicts import VerdictEffect
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from reported_author import author_for_features, content_id_for_features

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
    """

    timeout: float = 5.0

    def __init__(self) -> None:
        self._url = os.environ.get('DIVINE_COOP_URL', '')
        self._api_key = os.environ.get('DIVINE_COOP_API_KEY', '')
        self._content_type = os.environ.get('DIVINE_COOP_CONTENT_TYPE', 'nostr_event')

        if not self._url:
            logger.info('DIVINE_COOP_URL not set. COOPSink disabled.')
        elif not self._api_key:
            logger.warning('DIVINE_COOP_API_KEY not set. COOPSink will fail auth.')

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
                timeout=self.timeout,
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
