import os
from typing import Any

import requests
from media_hash import is_valid_media_hash, normalize_media_hash
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from reported_author import normalize_event_id
from udfs.age_restrict_nostr_event import AgeRestrictEffect
from udfs.ban_nostr_event import BanEventEffect

logger = get_logger(__name__)


def _loggable_event_id(value: object) -> str:
    """An event id safe to put in a log line.

    The id originates in a report's `e` tag or a label's, so it is third-party
    input and arrives here unvalidated: nothing between the bridge and this sink
    checks its shape. Echoed raw it is unbounded and can carry newlines, so a
    hostile value can forge log lines or bury the surrounding context.

    A well-formed id is returned as-is, since that is the overwhelmingly common
    case and operators match on it. Anything else is escaped and truncated rather
    than dropped, because the malformed value is exactly what a reader needs when
    working out why an enforcement looks wrong.

    This makes the LOG safe. It deliberately does not gate the enforcement call
    itself; see the note in `_ban_event`.
    """
    normalized = normalize_event_id(value)
    return normalized or f'<malformed: {value!r:.64}>'


class RelayManagerSink(BaseOutputSink):
    """Output sink that sends moderation actions to Divine's relay-manager.

    Supports ``banevent`` (content removal) and ``banpubkey`` (user ban) via the
    ``/api/relay-rpc`` JSON-RPC endpoint, and ``AGE_RESTRICTED`` via
    ``/api/moderate-media``.

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

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {'Content-Type': 'application/json'}
        if self._api_key:
            h['X-Admin-Key'] = self._api_key
        return h

    def will_do_work(self, result: ExecutionResult) -> bool:
        if not self._url:
            return False
        has_bans = len(result.effects.get(BanEventEffect, [])) > 0
        has_restricts = len(result.effects.get(AgeRestrictEffect, [])) > 0
        return has_bans or has_restricts

    def push(self, result: ExecutionResult) -> None:
        ban_effects: list[BanEventEffect] = result.effects.get(BanEventEffect, [])
        for effect in ban_effects:
            assert isinstance(effect, BanEventEffect)
            self._ban_event(effect)

            if effect.pubkey:
                self._ban_pubkey(effect)

            # Only publish the enforcement label after all required bans succeed.
            # Both bans are idempotent so replaying on retry is safe. Label publish
            # is NOT idempotent (creates a new signed event each time), so retries
            # may produce duplicate labels -- acceptable since losing the audit
            # trail is worse than duplicating it.
            try:
                self._publish_label_event(effect)
            except Exception:
                logger.error(
                    'Enforcement label failed for %s -- bans succeeded, label lost',
                    _loggable_event_id(effect.event_id),
                )
                raise

        restrict_effects: list[AgeRestrictEffect] = result.effects.get(AgeRestrictEffect, [])
        for effect in restrict_effects:
            assert isinstance(effect, AgeRestrictEffect)
            self._age_restrict_media(effect)

    def _ban_event(self, effect: BanEventEffect) -> None:
        # The id is sent to relay-manager AS GIVEN, deliberately, while the log
        # lines around it are sanitised.
        #
        # Sanitising a log line cannot change what is enforced. Validating here
        # could: a malformed id would stop being a relay-manager 400 and start
        # being a silent no-op on our side, which is the failure mode this whole
        # body of work exists to remove. Whether the sink should refuse to call on
        # a malformed id is a change in enforcement posture and belongs with the
        # rules that produce the id, not smuggled in behind a logging fix.
        payload: dict[str, Any] = {
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
            logger.info('Banned event %s via relay-manager', _loggable_event_id(effect.event_id))
        except Exception:
            logger.exception('Failed to ban event %s via relay-manager', _loggable_event_id(effect.event_id))
            raise

    def _ban_pubkey(self, effect: BanEventEffect) -> None:
        payload: dict[str, Any] = {
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
            logger.info('Banned pubkey %s via relay-manager', effect.pubkey)
        except Exception:
            logger.exception('Failed to ban pubkey %s via relay-manager', effect.pubkey)
            raise

    def _publish_label_event(self, effect: BanEventEffect) -> None:
        """Publish a Kind 1985 label event as an audit trail for the ban action.

        Uses moderation/enforcement namespace (not moderation/resolution) so
        relay-manager doesn't treat it as a human review decision.
        """
        tags: list[list[str]] = [
            ['L', 'moderation/enforcement'],
            ['l', 'auto_hidden', 'moderation/enforcement'],
            ['e', effect.event_id],
        ]
        if effect.pubkey:
            tags.append(['p', effect.pubkey])

        payload: dict[str, Any] = {
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
            logger.info('Published enforcement label for event %s', _loggable_event_id(effect.event_id))
        except Exception:
            logger.exception('Failed to publish enforcement label for event %s', _loggable_event_id(effect.event_id))
            raise

    def _age_restrict_media(self, effect: AgeRestrictEffect) -> None:
        # Normalise before validating and before sending. Validation accepts
        # uppercase on purpose, since case must not decide whether a moderator's
        # decision is enforced, but relay-manager forwards the value to
        # moderation-service, which stores it as sent and compares it
        # case-sensitively. An uppercase hash therefore opens a SECOND row for the
        # same media instead of updating the first, skipping the relay
        # notification and missing the dashboard and creator-DM lookups. See
        # media_hash.py.
        #
        # The rules gate enforcement on IsValidMediaHash, so reaching the check
        # below with a malformed hash means rule and sink have diverged. That is
        # worth an ERROR rather than a warning: skipping the call while the rules
        # have already written `age_restricted` and declared `restrict` leaves
        # Osprey's record asserting a restriction that never happened. Kept as a
        # backstop because the enforcement call is the thing that must not fire on
        # bad input.
        sha256 = normalize_media_hash(effect.sha256)
        if not is_valid_media_hash(sha256):
            logger.error(
                'Skipping age-restrict for event %s: malformed sha256 %s reached the sink, '
                'which the rules should have routed to review. Osprey now records a '
                'restriction that was NOT applied.',
                _loggable_event_id(effect.event_id) if effect.event_id else '<no target>',
                # repr, bounded: the raw value is what a reader needs in order to
                # diagnose this, but it arrives from a third-party label and may
                # be None, non-string, or long. repr never raises and escapes
                # control characters; the slice bounds it. Slicing the raw value
                # directly is what made this line crash on exactly the input it
                # exists to report.
                repr(effect.sha256)[:64],
            )
            return
        payload: dict[str, Any] = {
            'sha256': sha256,
            'action': 'AGE_RESTRICTED',
            'reason': effect.reason,
        }
        try:
            resp = requests.post(
                f'{self._url}/api/moderate-media',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            # Log what was SENT, not what arrived: these lines are how an operator
            # reconciles Osprey against moderation-service's records, and the
            # normalised value is the one that exists on the far side.
            logger.info('Age-restricted media %s for event %s', sha256, _loggable_event_id(effect.event_id))
        except Exception:
            logger.exception(
                'Failed to age-restrict media %s for event %s', sha256, _loggable_event_id(effect.event_id)
            )
            raise

    def stop(self) -> None:
        pass
