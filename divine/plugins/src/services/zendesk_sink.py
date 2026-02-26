import os
from typing import Any, Dict

from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger(__name__)

DEFAULT_ZENDESK_URL = 'https://example.zendesk.com'


class ZendeskSink(BaseOutputSink):
    """Output sink that creates/updates Zendesk tickets from rule verdicts.

    This is a stub implementation. Configure via environment variables:
      - DIVINE_ZENDESK_URL: Zendesk API base URL
      - DIVINE_ZENDESK_TOKEN: API token for authentication
      - DIVINE_ZENDESK_EMAIL: Agent email for authentication
    """

    timeout: float = 5.0
    max_retries: int = 1

    def __init__(self, zendesk_url: str | None = None) -> None:
        self._url = zendesk_url or os.environ.get('DIVINE_ZENDESK_URL', DEFAULT_ZENDESK_URL)
        self._token = os.environ.get('DIVINE_ZENDESK_TOKEN', '')
        self._email = os.environ.get('DIVINE_ZENDESK_EMAIL', '')

    def will_do_work(self, result: ExecutionResult) -> bool:
        # Process any result that has verdicts
        return len(result.verdicts) > 0

    def push(self, result: ExecutionResult) -> None:
        # TODO: Implement actual Zendesk API calls
        # For each verdict, create or update a ticket
        for verdict in result.verdicts:
            ticket_data: Dict[str, Any] = {
                'ticket': {
                    'subject': f'Moderation verdict: {verdict}',
                    'description': f'Automated moderation verdict from Osprey rules engine.\n\n'
                                   f'Features: {result.extracted_features_json}',
                    'priority': 'normal',
                    'tags': ['divine-moderation', 'automated'],
                }
            }
            logger.info(f'Would create Zendesk ticket: {ticket_data}')
            # TODO: POST to {self._url}/api/v2/tickets.json with auth

    def stop(self) -> None:
        pass
