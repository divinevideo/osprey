"""Tests for the configurable trusted-moderation-signer allowlist.

These cover the pure env-parsing helper rather than the UDF wrapper, so they run
without the Osprey engine installed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from trusted_moderation import (  # noqa: E402
    PRODUCTION_MODERATION_PUBKEY,
    is_trusted_moderation_signer,
    trusted_moderation_pubkeys,
)

OTHER = 'a' * 64
UPPER = 'B' * 64


def test_defaults_to_production_pubkey_when_unset():
    """Prod must keep working with no env var set, so the default is not empty."""
    assert trusted_moderation_pubkeys({}) == frozenset({PRODUCTION_MODERATION_PUBKEY})


def test_defaults_when_set_but_blank():
    assert trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': '   '}) == frozenset(
        {PRODUCTION_MODERATION_PUBKEY}
    )


def test_override_replaces_the_default_entirely():
    """Staging must NOT implicitly keep trusting the production identity."""
    keys = trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': OTHER})
    assert keys == frozenset({OTHER})
    assert PRODUCTION_MODERATION_PUBKEY not in keys


def test_accepts_a_comma_separated_list_for_rotation():
    keys = trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': f'{OTHER}, {PRODUCTION_MODERATION_PUBKEY}'})
    assert keys == frozenset({OTHER, PRODUCTION_MODERATION_PUBKEY})


def test_normalizes_case_and_whitespace():
    keys = trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': f'  {UPPER}  '})
    assert keys == frozenset({UPPER.lower()})


@pytest.mark.parametrize(
    'bad',
    [
        'not-hex',
        'abc',  # too short
        'a' * 63,
        'a' * 65,
        'g' * 64,  # non-hex character
        'npub1' + 'a' * 58,  # bech32, not hex
    ],
)
def test_drops_malformed_entries(bad):
    keys = trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': f'{OTHER},{bad}'})
    assert keys == frozenset({OTHER})


def test_fails_closed_when_every_entry_is_malformed():
    """An operator typo must not silently fall back to trusting production.

    Trusting nobody means no automated enforcement fires, which is the safe
    direction for an enforcement gate.
    """
    assert trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': 'garbage,also-garbage'}) == frozenset()


def test_is_trusted_matches_and_rejects():
    env = {'DIVINE_TRUSTED_MODERATION_PUBKEYS': OTHER}
    assert is_trusted_moderation_signer(OTHER, env)
    assert not is_trusted_moderation_signer(PRODUCTION_MODERATION_PUBKEY, env)


def test_is_trusted_normalizes_signer_case():
    assert is_trusted_moderation_signer(UPPER, {'DIVINE_TRUSTED_MODERATION_PUBKEYS': UPPER.lower()})


@pytest.mark.parametrize('empty', ['', None])
def test_is_trusted_rejects_missing_signer(empty):
    """A label with no resolvable signer must never satisfy the gate."""
    assert not is_trusted_moderation_signer(empty, {})
