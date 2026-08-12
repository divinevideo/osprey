"""Configurable allowlist of pubkeys whose kind-1985 labels Osprey will act on.

The label-routing rules gate enforcement on the *signer* of the label event, not
on its `source` metadata field, because that metadata is attacker-controlled.
The signing identity differs per environment, so hardcoding it in SML made the
rules unexercisable outside production: no label signed by the production
moderation key has ever reached the staging relay, so the rules could never fire
there, and any attempt to validate them on staging would silently do nothing.

Set `DIVINE_TRUSTED_MODERATION_PUBKEYS` to a comma-separated list of 64-char hex
pubkeys. Unset, it defaults to the production identity, so production behaviour
is unchanged and needs no deployment change.

Deliberate behaviours:

- An override *replaces* the default rather than adding to it. Staging must not
  implicitly keep trusting production.
- Malformed entries are dropped. If that leaves nothing, the result is empty and
  no label is trusted, so enforcement stops rather than falling back to a key the
  operator did not intend. For an enforcement gate, failing closed is the safe
  direction.
- Accepting a list rather than a single value means the moderation identity can
  be rotated by trusting both keys through the changeover, instead of requiring a
  hard cutover deploy.

This module imports nothing from Osprey so it can be unit tested without the
engine installed.
"""

import logging
import os
from functools import lru_cache
from typing import FrozenSet, Mapping, Optional

from media_hash import HEX64_RE

logger = logging.getLogger(__name__)

# NIP-05 moderation@divine.video. Verified against the live well-known 2026-07-30.
PRODUCTION_MODERATION_PUBKEY = '8fd5eb6d8f362163bc00a5ab6b4a3167dbf32d00ec4efdbcf43b3c9514433b7e'

ENV_VAR = 'DIVINE_TRUSTED_MODERATION_PUBKEYS'


def _normalize(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ''
    return value.strip().lower()


@lru_cache(maxsize=8)
def _parse(raw: str) -> FrozenSet[str]:
    """Parse the raw env value. Cached because this runs on every label event.

    The cache also makes the logging below fire once per distinct value rather
    than once per event, so this is effectively startup-time reporting.
    """
    if not raw.strip():
        logger.info('%s unset, trusting the production moderation identity only', ENV_VAR)
        return frozenset({PRODUCTION_MODERATION_PUBKEY})

    entries = [_normalize(entry) for entry in raw.split(',') if _normalize(entry)]
    accepted = frozenset(entry for entry in entries if HEX64_RE.match(entry))
    rejected = [entry for entry in entries if not HEX64_RE.match(entry)]

    if rejected:
        # Values are operator-supplied config, not attacker input, but they are
        # still truncated: a mistyped entry could be a pasted secret.
        logger.warning(
            '%s: dropped %d malformed entr%s (not 64-char hex): %s',
            ENV_VAR,
            len(rejected),
            'y' if len(rejected) == 1 else 'ies',
            ', '.join(repr(entry[:8]) for entry in rejected),
        )

    if not accepted:
        # Fail-closed is the right direction for an enforcement gate -- falling
        # back to a key the operator did not intend would be worse. But silence
        # is not: this disables EVERY label-driven rule, including the CSAM ban
        # path, and the only symptom is that nothing ever happens.
        logger.error(
            '%s is set but yielded no valid pubkeys -- ALL label-driven enforcement '
            'is now disabled, including CSAM. Fix the value or unset it to fall back '
            'to the production moderation identity.',
            ENV_VAR,
        )
    else:
        logger.info(
            '%s: trusting %d moderation identit%s', ENV_VAR, len(accepted), 'y' if len(accepted) == 1 else 'ies'
        )

    return accepted


def trusted_moderation_pubkeys(env: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """Return the set of pubkeys whose moderation labels may drive enforcement."""
    source: Mapping[str, str] = os.environ if env is None else env
    return _parse(source.get(ENV_VAR, '') or '')


def is_trusted_moderation_signer(pubkey: Optional[str], env: Optional[Mapping[str, str]] = None) -> bool:
    """True when `pubkey` signed as a trusted moderation identity for this environment."""
    normalized = _normalize(pubkey)
    if not HEX64_RE.match(normalized):
        return False
    return normalized in trusted_moderation_pubkeys(env)
