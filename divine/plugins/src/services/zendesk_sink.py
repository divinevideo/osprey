import os
from typing import Any, Dict

from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger(__name__)

# REVIEW NOTE (matt, 2026-03-06):
#
# Divine's Zendesk is at rabblelabs.zendesk.com. Auth is email/token
# (not OAuth). The existing relay-manager Zendesk integration uses a
# bi-directional sync pattern:
#   - Incoming: Zendesk webhook creates GitHub issues for Problem tickets
#   - Outgoing: moderation actions resolve matching Zendesk tickets
#
# For Osprey, the Zendesk sink should probably:
#   1. Only create tickets for verdicts that need human follow-up
#      (flag_for_review, escalations). Auto-bans don't need tickets.
#   2. Resolve existing tickets when a verdict closes an open report.
#      relay-manager's syncZendeskAfterAction() has the resolution logic.
#   3. Use Zendesk Problem/Incident ticket pattern (one Problem per
#      reported user/content, incidents linked to it).
#
# Not every verdict should create a ticket. will_do_work() currently
# fires on any verdict, which would flood Zendesk with SAFE results.

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
