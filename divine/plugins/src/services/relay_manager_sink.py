import os
from dataclasses import replace
from typing import Any

import requests
from media_hash import is_valid_media_hash, normalize_media_hash
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink
from reported_author import normalize_event_id
from response_guard import require_json_response
from udfs.age_restrict_nostr_event import AgeRestrictEffect
from udfs.ban_nostr_event import BanEventEffect

logger = get_logger(__name__)


def _loggable_hex64(value: object) -> str:
    """A 64-hex identifier safe to put in a log line.

    An event id originates in a report's `e` tag or a label's, so it is third-party
    input and arrives here unvalidated: nothing between the bridge and this sink
    checks its shape. Echoed raw it is unbounded and can carry newlines, so a
    hostile value can forge log lines or bury the surrounding context.

    A well-formed id is returned as-is, since that is the overwhelmingly common
    case and operators match on it. Anything else is escaped and truncated rather
    than dropped, because the malformed value is exactly what a reader needs when
    working out why an enforcement looks wrong.

    Used for pubkeys too. test_enforcement_targets.py limits `pubkey=` to '',
    `Pubkey` or `ReportedAuthorPubkey`, and only the latter is _hex64-validated;
    `Pubkey` comes off the bridge unvalidated in this repo and relies on the relay
    having verified the signature. That is a thin and undocumented coupling to
    lean on for log safety, so the same treatment is applied here.

    This makes the LOG safe. Enforcement is gated separately, in `push`, which
    refuses to ban or sign on an id that is not well-formed.
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
      - ``CF_ACCESS_CLIENT_ID`` / ``CF_ACCESS_CLIENT_SECRET``: Required wherever
        relay-manager sits behind an edge authenticator, which is every deployed
        environment. Set **both or neither**. Without them the request never
        reaches relay-manager: the edge answers with an auth page carrying a 2xx,
        which is why ``_request`` checks the body rather than the status alone.
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
        # A HALF-configured edge token is the dangerous state: neither half set is
        # correct for local dev, and both set is correct everywhere deployed, but
        # exactly one -- a typo'd secretKey, a partial ExternalSecret sync -- boots
        # looking identical to local dev while every enforcement call is refused
        # by the edge. Say so at startup rather than leaving it to be inferred
        # from failures later.
        cf_id = bool(os.environ.get('CF_ACCESS_CLIENT_ID', '').strip())
        cf_secret = bool(os.environ.get('CF_ACCESS_CLIENT_SECRET', '').strip())
        if cf_id != cf_secret:
            present = 'CF_ACCESS_CLIENT_ID' if cf_id else 'CF_ACCESS_CLIENT_SECRET'
            missing = 'CF_ACCESS_CLIENT_SECRET' if cf_id else 'CF_ACCESS_CLIENT_ID'
            logger.warning(
                '%s is set but %s is not. The edge service token is sent only as a pair, so '
                'no token will be sent and every enforcement call will be refused.',
                present,
                missing,
            )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {'Content-Type': 'application/json'}
        if self._api_key:
            h['X-Admin-Key'] = self._api_key
        # relay-manager sits behind an edge authenticator in the deployed
        # environments. Both halves or neither: sending one is not a partial
        # credential, it is an unauthenticated request that the edge answers with
        # an auth page, and a half-configured deployment must not be mistaken for
        # an authenticated one.
        client_id = os.environ.get('CF_ACCESS_CLIENT_ID', '').strip()
        client_secret = os.environ.get('CF_ACCESS_CLIENT_SECRET', '').strip()
        if client_id and client_secret:
            h['CF-Access-Client-Id'] = client_id
            h['CF-Access-Client-Secret'] = client_secret
        return h

    def _request(self, path: str, payload: dict[str, Any]) -> None:
        """The ONLY place this module issues an HTTP request.

        Single site on purpose. The response validation below was added after
        every effect handler silently no-opped for want of it, and patching the
        handlers that existed at the time would have left the next one exposed to
        the same failure. Route new calls through here rather than adding a second
        site; test_sink_request_discipline.py enforces that.
        """
        url = f'{self._url}{path}'
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        # raise_for_status cannot see a 2xx that is not the API. An unauthenticated
        # request to an edge-protected endpoint comes back 200 with an auth page,
        # and treating that as success is what made enforcement silent.
        # Full body, NOT a prefix: the guard parses it, and a truncated body can
        # never parse, so slicing here would reject every response as malformed.
        # The guard bounds the excerpt it puts in the message itself.
        require_json_response(url, resp.status_code, resp.headers.get('Content-Type', ''), resp.text)

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

            # Gate the ENFORCEMENT on a well-formed id, not just the log line.
            #
            # This was previously left open on purpose and filed for a decision, on the
            # grounds that "the rules reaching BanNostrEvent gate on a trusted reporter,
            # a trusted moderation signer, or a distinct-reporter threshold, so this is
            # not open to any user." That premise stopped being true when csam began
            # acting on a single report from ANY reporter
            # (rules/reports/first_report_review.sml). The decision has now been taken:
            # refuse.
            #
            # The id originates in a report's `e` tag and is entirely attacker-chosen.
            # Unvalidated it reaches the relay RPC and, worse, `_publish_label_event`,
            # which SIGNS a kind-1985 carrying it with Divine's moderation key and
            # broadcasts it -- a durable, publicly attributable artifact we did not
            # author. Skipping is the safe failure: a ban we decline to issue is
            # recoverable, a signature we publish is not.
            event_id = normalize_event_id(effect.event_id)
            if not event_id:
                logger.error(
                    'ALERT: refusing to enforce on a malformed event id %s; no ban issued and no label signed',
                    _loggable_hex64(effect.event_id),
                )
                continue

            # Carry the NORMALIZED id forward. _hex64 lowercases, and this sink talks to
            # /api/relay-rpc, which does not canonicalise -- so an uppercase id would
            # otherwise ban a different key than the one everything else refers to.
            effect = replace(effect, event_id=event_id)

            self._ban_event(effect)

            if effect.pubkey:
                # banpubkey PURGES irreversibly. Validate before issuing it, and on a
                # malformed pubkey skip ONLY the account ban: the event ban above is
                # legitimate and reversible, so dropping it too would be a worse
                # outcome than a partial enforcement that says so loudly.
                if not normalize_event_id(effect.pubkey):
                    logger.error(
                        'ALERT: refusing to ban a malformed pubkey %s; the event ban '
                        'stands, the account ban was NOT issued',
                        _loggable_hex64(effect.pubkey),
                    )
                else:
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
                    _loggable_hex64(effect.event_id),
                )
                raise

        restrict_effects: list[AgeRestrictEffect] = result.effects.get(AgeRestrictEffect, [])
        for effect in restrict_effects:
            assert isinstance(effect, AgeRestrictEffect)
            self._age_restrict_media(effect)

    def _ban_event(self, effect: BanEventEffect) -> None:
        # The id is sent AS GIVEN while the log lines around it are sanitised, and
        # that split is NOT currently justified by anything downstream. Nothing
        # validates it, verified rather than assumed:
        #
        #   - relay-manager's `/^[0-9a-f]{64}$/` check is scoped to `banpubkey` and
        #     `suspendpubkey` (worker/src/index.ts ~1102). `banevent` falls through.
        #   - funnelcake's BanEvent takes params[0] and hands it to
        #     `storage.ban_event` with no hex check, and answers ok.
        #   - `handlePublish` validates only `kind` and `content` before
        #     `finalizeEvent(..., secretKey)`, so whatever lands in the `['e', ...]`
        #     tag below is SIGNED with the moderation key and broadcast.
        #
        # So a malformed id is written into funnelcake's banned-events list matching
        # no event, this sink logs a successful ban, Osprey records the enforcement,
        # and a kind-1985 carrying the same garbage goes out over Divine's
        # signature. That is the silent-no-op class this work exists to remove, one
        # hop further out, and now also durable and attributable.
        #
        # It is left unchanged here on purpose. Refusing to call on a malformed id
        # is a change in enforcement posture, and the last time such a change rode
        # along inside a hygiene fix it had to be reverted. It is filed for a
        # decision instead. Reachability is limited: the rules reaching
        # BanNostrEvent gate on a trusted reporter, a trusted moderation signer, or
        # a distinct-reporter threshold, so this is not open to any user.
        payload: dict[str, Any] = {
            'method': 'banevent',
            'params': [effect.event_id, effect.reason],
        }
        try:
            self._request('/api/relay-rpc', payload)
            logger.info('Banned event %s via relay-manager', _loggable_hex64(effect.event_id))
        except Exception:
            logger.exception('Failed to ban event %s via relay-manager', _loggable_hex64(effect.event_id))
            raise

    def _ban_pubkey(self, effect: BanEventEffect) -> None:
        payload: dict[str, Any] = {
            'method': 'banpubkey',
            'params': [effect.pubkey, effect.reason],
        }
        try:
            self._request('/api/relay-rpc', payload)
            logger.info('Banned pubkey %s via relay-manager', _loggable_hex64(effect.pubkey))
        except Exception:
            logger.exception('Failed to ban pubkey %s via relay-manager', _loggable_hex64(effect.pubkey))
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
            self._request('/api/publish', payload)
            logger.info('Published enforcement label for event %s', _loggable_hex64(effect.event_id))
        except Exception:
            logger.exception('Failed to publish enforcement label for event %s', _loggable_hex64(effect.event_id))
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
                _loggable_hex64(effect.event_id) if effect.event_id else '<no target>',
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
            self._request('/api/moderate-media', payload)
            # Log what was SENT, not what arrived: these lines are how an operator
            # reconciles Osprey against moderation-service's records, and the
            # normalised value is the one that exists on the far side.
            logger.info('Age-restricted media %s for event %s', sha256, _loggable_hex64(effect.event_id))
        except Exception:
            logger.exception('Failed to age-restrict media %s for event %s', sha256, _loggable_hex64(effect.event_id))
            raise

    def stop(self) -> None:
        pass
