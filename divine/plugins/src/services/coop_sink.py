import os
from typing import Any

import requests
import sentry_sdk
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.engine.language_types.verdicts import VerdictEffect
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

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
        """Resolve the actual moderated content ID, not the wrapper event.

        Kind 1984 reports and kind 1985 labels wrap a target event; EventId
        is the wrapper. COOP should key on the moderated content instead.
        Returns None if no real event ID is available.
        """
        for key in ('LabelTargetEvent', 'ReportedEventId', 'EventId'):
            value = features.get(key)
            if value:
                return str(value)
        return None

    def _submit_content(self, content_id: str, verdict: VerdictEffect, result: ExecutionResult) -> None:
        features = result.extracted_features
        wrapper_event_id = features.get('EventId', str(result.action.action_id))

        content: dict[str, Any] = {
            'event_id': content_id,
            'source_event_id': wrapper_event_id,
            'pubkey': features.get('Pubkey', ''),
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

        reported_pubkey = features.get('ReportedPubkey')
        user_id = str(reported_pubkey) if reported_pubkey else features.get('Pubkey', '')

        payload: dict[str, Any] = {
            'contentId': content_id,
            'contentType': self._content_type,
            'userId': user_id,
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
