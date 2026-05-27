import os
from typing import Any, Dict

import requests
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.engine.language_types.verdicts import VerdictEffect
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger(__name__)

ACTIONABLE_VERDICTS = {'flag_for_review', 'restrict', 'auto_hide', 'ban'}


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
    max_retries: int = 2

    def __init__(self) -> None:
        self._url = os.environ.get('DIVINE_COOP_URL', '')
        self._api_key = os.environ.get('DIVINE_COOP_API_KEY', '')
        self._content_type = os.environ.get('DIVINE_COOP_CONTENT_TYPE', 'nostr_event')

        if not self._url:
            logger.info('DIVINE_COOP_URL not set. COOPSink disabled.')
        elif not self._api_key:
            logger.warning('DIVINE_COOP_API_KEY not set. COOPSink will fail auth.')

    def _headers(self) -> Dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'x-api-key': self._api_key,
        }

    def will_do_work(self, result: ExecutionResult) -> bool:
        if not self._url or not self._api_key:
            return False
        return any(v.verdict.lower() in ACTIONABLE_VERDICTS for v in result.verdicts)

    def push(self, result: ExecutionResult) -> None:
        for verdict in result.verdicts:
            if verdict.verdict.lower() in ACTIONABLE_VERDICTS:
                self._submit_content(verdict, result)

    def _submit_content(self, verdict: VerdictEffect, result: ExecutionResult) -> None:
        features = result.extracted_features
        event_id = features.get('EventId', str(result.action.action_id))

        content: Dict[str, Any] = {
            'event_id': features.get('EventId', ''),
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

        payload: Dict[str, Any] = {
            'contentId': event_id,
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
            logger.info(f'Submitted to COOP: event={event_id} verdict={verdict.verdict} kind={features.get("Kind")}')
        except Exception:
            logger.exception(f'Failed to submit to COOP: event={event_id} verdict={verdict.verdict}')
            raise

    def stop(self) -> None:
        pass
