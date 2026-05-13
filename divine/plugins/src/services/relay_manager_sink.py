import os
from typing import Any, Dict, List

import requests
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from udfs.ban_nostr_event import BanEventEffect

logger = get_logger(__name__)


class RelayManagerSink(BaseOutputSink):
    """Output sink that sends ban actions to Divine's relay-manager NIP-86 endpoint.

    Supports both ``banevent`` (content removal) and ``banpubkey`` (user ban)
    via the ``/api/relay-rpc`` JSON-RPC endpoint.

    Configuration (environment variables):
      - ``DIVINE_RELAY_MANAGER_URL``: Required. Base URL of the relay-manager
        worker (e.g. ``https://api-relay-prod.divine.video``).
      - ``DIVINE_RELAY_MANAGER_API_KEY``: Required. Value for the ``X-Admin-Key``
        header. Must match the ``ADMIN_API_KEY`` secret on the target worker.
    """

    timeout: float = 5.0
    max_retries: int = 2

    def __init__(self, relay_manager_url: str | None = None, api_key: str | None = None) -> None:
        self._url = relay_manager_url or os.environ.get('DIVINE_RELAY_MANAGER_URL', '')
        self._api_key = api_key or os.environ.get('DIVINE_RELAY_MANAGER_API_KEY', '')
        if not self._url:
            logger.warning('DIVINE_RELAY_MANAGER_URL not set. RelayManagerSink will skip all effects.')
        if not self._api_key:
            logger.warning('DIVINE_RELAY_MANAGER_API_KEY not set. Requests will fail auth.')

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {'Content-Type': 'application/json'}
        if self._api_key:
            h['X-Admin-Key'] = self._api_key
        return h

    def will_do_work(self, result: ExecutionResult) -> bool:
        if not self._url:
            return False
        return len(result.effects.get(BanEventEffect, [])) > 0

    def push(self, result: ExecutionResult) -> None:
        effects: List[BanEventEffect] = result.effects.get(BanEventEffect, [])
        for effect in effects:
            assert isinstance(effect, BanEventEffect)
            event_banned = False
            try:
                self._ban_event(effect)
                event_banned = True
            except Exception:
                pass  # Already logged in _ban_event

            if effect.pubkey:
                try:
                    self._ban_pubkey(effect)
                except Exception:
                    pass  # Already logged in _ban_pubkey

            if event_banned:
                try:
                    self._publish_label_event(effect)
                except Exception:
                    # Ban succeeded but audit label is lost. Log at error so
                    # this is visible in alerting. Future: write to a dead-letter
                    # table for retry (event_id, pubkey, reason, tags, timestamp).
                    logger.error(f'Failed to publish enforcement label for {effect.event_id} — ban succeeded, label lost')

            if not event_banned:
                raise RuntimeError(f'Failed to ban event {effect.event_id}')

    def _ban_event(self, effect: BanEventEffect) -> None:
        payload: Dict[str, Any] = {
            'method': 'banevent',
            'params': [effect.event_id, effect.reason],
        }
        try:
            resp = requests.post(
                f'{self._url}/api/relay-rpc',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            logger.info(f'Banned event {effect.event_id} via relay-manager')
        except Exception:
            logger.exception(f'Failed to ban event {effect.event_id} via relay-manager')
            raise

    def _ban_pubkey(self, effect: BanEventEffect) -> None:
        payload: Dict[str, Any] = {
            'method': 'banpubkey',
            'params': [effect.pubkey, effect.reason],
        }
        try:
            resp = requests.post(
                f'{self._url}/api/relay-rpc',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            logger.info(f'Banned pubkey {effect.pubkey} via relay-manager')
        except Exception:
            logger.exception(f'Failed to ban pubkey {effect.pubkey} via relay-manager')
            raise

    def _publish_label_event(self, effect: BanEventEffect) -> None:
        """Publish a Kind 1985 label event as an audit trail for the ban action.

        Uses moderation/enforcement namespace (not moderation/resolution) so
        relay-manager doesn't treat it as a human review decision.
        """
        tags: List[List[str]] = [
            ['L', 'moderation/enforcement'],
            ['l', 'auto_hidden', 'moderation/enforcement'],
            ['e', effect.event_id],
        ]
        if effect.pubkey:
            tags.append(['p', effect.pubkey])

        payload: Dict[str, Any] = {
            'kind': 1985,
            'content': effect.reason,
            'tags': tags,
        }
        try:
            resp = requests.post(
                f'{self._url}/api/publish',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            logger.info(f'Published enforcement label for event {effect.event_id}')
        except Exception:
            logger.exception(f'Failed to publish enforcement label for event {effect.event_id}')
            raise

    def stop(self) -> None:
        pass
