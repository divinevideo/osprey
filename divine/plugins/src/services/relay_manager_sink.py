import os
from typing import Any, Dict

import requests
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from udfs.ban_nostr_event import BanEventEffect

logger = get_logger(__name__)

# REVIEW NOTE (matt, 2026-03-06):
#
# Connectivity: relay-manager is a Cloudflare Worker (api-relay-prod.divine.video),
# not a K8s service. Osprey runs in GKE. This default URL won't resolve in-cluster.
# Options:
#   1. Set DIVINE_RELAY_MANAGER_URL to the CF Worker's public URL
#      (needs auth -- the /api/relay-rpc endpoint has no token check today,
#      but going through CF's proxy from GKE means public internet egress)
#   2. Port relay-manager to GKE (big change)
#   3. Skip relay-manager, call Funnelcake's NIP-86 API directly
#      (needs the admin nsec to sign NIP-98 auth events)
#
# Payload format: {"method": "banevent", "params": [event_id, reason]} is correct
# and matches relay-manager's handleRelayRpc() handler.
#
# Missing: no banpubkey support. The repeat_offender rule wants to ban a user,
# not just one event. relay-manager supports both "banevent" and "banpubkey"
# as separate RPC methods. Consider adding a BanPubkeyEffect + handling here.
#
# Missing: no Blossom notification. Today relay-manager and moderation-service
# both call Blossom /admin/moderate to block media. If Osprey replaces those
# enforcement paths, this sink (or a new BlossomSink) needs to handle media too.
#
# max_retries is declared but not used.

DEFAULT_RELAY_MANAGER_URL = 'http://relay-manager.default.svc:5000'


class RelayManagerSink(BaseOutputSink):
    """Output sink that consumes BanEventEffect and POSTs to the
    relay-manager NIP-86 banevent endpoint.

    Requires the ``DIVINE_RELAY_MANAGER_URL`` environment variable to be set
    to the relay-manager base URL (e.g. ``http://relay-manager.default.svc:5000``).
    If unset, falls back to DEFAULT_RELAY_MANAGER_URL which assumes in-cluster
    Kubernetes service DNS.
    """

    timeout: float = 5.0
    max_retries: int = 2

    def __init__(self, relay_manager_url: str | None = None) -> None:
        self._url = relay_manager_url or os.environ.get('DIVINE_RELAY_MANAGER_URL', DEFAULT_RELAY_MANAGER_URL)

    def will_do_work(self, result: ExecutionResult) -> bool:
        return len(result.effects.get(BanEventEffect, [])) > 0

    def push(self, result: ExecutionResult) -> None:
        effects = result.effects.get(BanEventEffect, [])
        for effect in effects:
            assert isinstance(effect, BanEventEffect)
            payload: Dict[str, Any] = {
                'method': 'banevent',
                'params': [effect.event_id, effect.reason],
            }
            try:
                resp = requests.post(
                    f'{self._url}/api/relay-rpc',
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                logger.info(f'Banned event {effect.event_id} via relay-manager')
            except Exception:
                logger.exception(f'Failed to ban event {effect.event_id} via relay-manager')
                raise

    def stop(self) -> None:
        pass
