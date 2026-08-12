import re

# A media sha256 is exactly 64 hexadecimal characters. relay-manager's
# moderate-media endpoint rejects anything else, so callers (the moderation-result
# check and the age-restrict sink) validate against this before sending a hash.
# `\Z`, not `$`: in Python `$` also matches immediately before a trailing
# newline, so `^[0-9a-f]{64}$` accepts "<64 hex>\n". That value passes validation
# and is then rejected downstream -- and a trailing newline is exactly what a
# pasted or file-sourced value carries.
HEX64_RE = re.compile(r'\A[0-9a-f]{64}\Z', re.IGNORECASE)


def is_valid_media_hash(value: object) -> bool:
    """True when `value` is a media sha256 the enforcement endpoint will accept.

    Used by the rules (via the IsValidMediaHash UDF) so a label carrying a
    malformed hash routes to review instead of declaring an enforcement that the
    sink would then decline to perform -- which would leave Osprey's record
    claiming a restriction that never happened.

    Deliberately the same check the sink applies, so the two cannot disagree.
    """
    return isinstance(value, str) and bool(HEX64_RE.match(value))


def normalize_media_hash(value: object) -> str:
    """The canonical spelling of `value`, or '' when it is not a usable hash.

    Validation accepts uppercase deliberately: a moderator's decision should not
    turn on the case of a hex string. Enforcement, however, must send one
    spelling. Blossom lowercases internally so an uppercase hash blocks the right
    media, but moderation-service stores the value as it was sent and compares it
    case-sensitively, so the uppercase spelling opens a SECOND row for the same
    media instead of updating the existing one. The relay notification keyed on
    that row is then skipped, and the dashboard and creator-DM lookups miss.

    Divergent records for a single piece of media is precisely the failure this
    work exists to remove, so callers normalise at the boundary where the value
    leaves us rather than trusting the far side to cope.

    Returns '' for anything unusable so that a caller reporting a rejected value
    cannot itself crash on it.
    """
    if not isinstance(value, str):
        return ''
    return value.lower()
