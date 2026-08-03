"""Tests for the media-hash validity check the enforcement rules gate on.

Pure helper, so these run without the Osprey engine installed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from media_hash import is_valid_media_hash  # noqa: E402

VALID = 'dd44' + '0' * 59 + '4'


def test_accepts_a_64_char_hex_hash():
    assert len(VALID) == 64
    assert is_valid_media_hash(VALID)


def test_accepts_uppercase():
    """Labels are third-party input; case should not decide enforcement."""
    assert is_valid_media_hash(VALID.upper())


@pytest.mark.parametrize(
    'bad',
    [
        None,          # tag absent entirely -- the common shape
        '',            # tag present but empty
        'a' * 63,      # one short: silently wrong, not obviously wrong
        'a' * 65,
        'g' * 64,      # right length, not hex
        'not-a-hash',
        '  ' + 'a' * 64,   # padded; the sink does not strip, so nor do we
        'a' * 64 + '\n',   # trailing newline, a common config/paste artifact
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
