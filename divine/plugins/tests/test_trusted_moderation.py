"""Tests for the configurable trusted-moderation-signer allowlist.

These cover the pure env-parsing helper rather than the UDF wrapper, so they run
without the Osprey engine installed.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from trusted_moderation import (  # noqa: E402
    PRODUCTION_MODERATION_PUBKEY,
    _parse,
    is_trusted_moderation_signer,
    trusted_moderation_pubkeys,
)


@pytest.fixture(autouse=True)
def _clear_parse_cache():
    """_parse is lru_cached, which would otherwise suppress the log assertions."""
    _parse.cache_clear()
    yield
    _parse.cache_clear()

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


# --- Operator-visibility of a fail-closed set ---
#
# Failing closed is right, but a silent fail-closed is indistinguishable from
# "no labels arrived". These assert the operator gets told.


def test_total_failure_logs_an_error_naming_the_consequence(caplog):
    with caplog.at_level(logging.ERROR, logger='trusted_moderation'):
        assert trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': 'typo'}) == frozenset()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert 'DIVINE_TRUSTED_MODERATION_PUBKEYS' in message
    # The point of the message is that enforcement is off, not merely that
    # parsing failed. Assert the consequence is stated.
    assert 'disabled' in message
    assert 'CSAM' in message


def test_partial_failure_warns_but_does_not_error(caplog):
    """One good key still enforces, so this is a warning, not an outage."""
    with caplog.at_level(logging.DEBUG, logger='trusted_moderation'):
        assert trusted_moderation_pubkeys(
            {'DIVINE_TRUSTED_MODERATION_PUBKEYS': f'{OTHER},typo'}
        ) == frozenset({OTHER})

    assert [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_rejected_entries_are_truncated_in_logs(caplog):
    """A mistyped entry could be a pasted secret; do not echo it whole."""
    secret = 'z' * 64
    with caplog.at_level(logging.WARNING, logger='trusted_moderation'):
        trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': f'{OTHER},{secret}'})

    logged = ' '.join(r.getMessage() for r in caplog.records)
    assert secret not in logged
    assert 'zzzzzzzz' in logged


def test_healthy_config_logs_no_warning_or_error(caplog):
    """Guards against the alert becoming noise operators learn to ignore."""
    with caplog.at_level(logging.DEBUG, logger='trusted_moderation'):
        trusted_moderation_pubkeys({'DIVINE_TRUSTED_MODERATION_PUBKEYS': OTHER})

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
