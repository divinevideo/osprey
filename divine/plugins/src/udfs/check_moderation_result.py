import os

import requests
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase
from osprey.worker.lib.osprey_shared.logging import get_logger

logger = get_logger(__name__)


class CheckModerationResultArguments(ArgumentsBase):
    video_hash: str
    """The sha256 hash of the video content to check."""


class CheckModerationResult(UDFBase[CheckModerationResultArguments, str]):
    """Queries the Divine moderation API for a video's classification result.

    Returns the action tier as a lowercase string matching the values used
    in ai_classification.sml rules: 'safe', 'review', 'age_restricted',
    'permanent_ban', or 'unknown' if the video hasn't been classified.

    Configuration (environment variable):
      - ``DIVINE_MODERATION_API_URL``: Base URL of the moderation API
        (default: ``https://moderation-api.divine.video``).

    The ``/check-result/{sha256}`` endpoint is public (no auth required).
    """

    _timeout: float = 3.0

    def execute(self, execution_context: ExecutionContext, arguments: CheckModerationResultArguments) -> str:
        video_hash = arguments.video_hash
        if not video_hash:
            return 'unknown'

        base_url = os.environ.get('DIVINE_MODERATION_API_URL', 'https://moderation-api.divine.video')

        try:
            resp = requests.get(
                f'{base_url}/check-result/{video_hash}',
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception(f'Failed to check moderation result for {video_hash[:16]}...')
            return 'unknown'

        if not data.get('moderated'):
            return 'unknown'

        # API returns uppercase (SAFE, REVIEW, AGE_RESTRICTED, PERMANENT_BAN).
        # Rules use lowercase.
        action = data.get('action', '')
        return action.lower() if action else 'unknown'
