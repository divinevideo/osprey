"""Tests for the media-hash validity check the enforcement rules gate on.

Pure helper, so these run without the Osprey engine installed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from media_hash import is_valid_media_hash, normalize_media_hash  # noqa: E402

VALID = 'dd44' + '0' * 59 + '4'
MIXED = 'DD44' + '0' * 59 + '4'


def test_accepts_a_64_char_hex_hash():
    assert len(VALID) == 64
    assert is_valid_media_hash(VALID)


def test_accepts_uppercase():
    """Labels are third-party input; case should not decide enforcement."""
    assert is_valid_media_hash(VALID.upper())


@pytest.mark.parametrize(
    'bad',
    [
        None,  # tag absent entirely -- the common shape
        '',  # tag present but empty
        'a' * 63,  # one short: silently wrong, not obviously wrong
        'a' * 65,
        'g' * 64,  # right length, not hex
        'not-a-hash',
        '  ' + 'a' * 64,  # padded; the sink does not strip, so nor do we
        'a' * 64 + '\n',  # trailing newline, a common config/paste artifact
    ],
)
def test_rejects_anything_not_actionable(bad):
    assert not is_valid_media_hash(bad)


@pytest.mark.parametrize('wrong_type', [123, 3.5, [], {}, object()])
def test_rejects_non_strings(wrong_type):
    """The bridge sets this field from tag data, so the type is not guaranteed."""
    assert not is_valid_media_hash(wrong_type)


def test_absent_and_malformed_are_indistinguishable():
    """The single review-path condition depends on this: neither is enforceable."""
    assert is_valid_media_hash(None) == is_valid_media_hash('garbage')


# --- normalisation -------------------------------------------------------
# Accepting uppercase (above) is deliberate: case must not decide whether a
# moderator's decision is enforced. But accepting it is only half the contract.
# Blossom lowercases internally, so an uppercase hash enforces correctly there,
# while moderation-service records the value as sent. Its comparison is
# case-sensitive, so the uppercase spelling creates a SECOND row for the same
# media rather than updating the existing one: the relay notification is skipped,
# and the dashboard and creator-DM lookups both miss. Divergent records for one
# piece of media is the exact failure this work exists to remove, so the value is
# normalised at the point it is sent.


def test_normalizes_uppercase_to_lowercase():
    assert normalize_media_hash(MIXED) == VALID


def test_leaves_an_already_lowercase_hash_untouched():
    assert normalize_media_hash(VALID) == VALID


def test_normalizing_is_idempotent():
    """Applied at one boundary today; re-applying must never change the value."""
    assert normalize_media_hash(normalize_media_hash(MIXED)) == normalize_media_hash(MIXED)


def test_a_normalized_hash_is_still_valid():
    """Normalisation must not turn an enforceable hash into an unenforceable one."""
    assert is_valid_media_hash(normalize_media_hash(MIXED))


@pytest.mark.parametrize('wrong_type', [None, 123, 3.5, [], {}, object()])
def test_returns_empty_string_for_a_non_string(wrong_type):
    """The bridge fills this field from tag data, so the type is not guaranteed.

    Returning '' rather than passing the input through means the sink's rejection
    log, and any caller doing string work on the result, cannot crash on the
    malformed input it exists to report.
    """
    assert normalize_media_hash(wrong_type) == ''


@pytest.mark.parametrize('malformed', ['garbage', 'NOT-A-HASH', 'a' * 63, 'g' * 64, '  ' + 'a' * 64])
def test_a_malformed_string_is_lowercased_not_emptied(malformed):
    """Normalisation is not validation, and conflating them loses information.

    This is the case the previous version of this test missed: it parametrised
    only non-strings and the empty string, so it could not tell "returns '' for
    anything unusable" (which the docstring claimed and the code never did) from
    "lowercases any string" (which is what actually happens).

    The behaviour is deliberate. The effect now carries the normalised value, so
    emptying a bad hash would destroy the only diagnostic the sink has when it
    refuses to enforce, and would make an unusable hash indistinguishable from an
    absent one. Validity is `is_valid_media_hash`'s job, and the sink runs both.
    """
    assert normalize_media_hash(malformed) == malformed.lower()
    assert not is_valid_media_hash(normalize_media_hash(malformed))
