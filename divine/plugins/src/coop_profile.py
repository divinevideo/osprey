"""Turns a funnelcake user lookup into the profile fields a moderator reads.

A review card that shows 64 hex characters where a person should be forces the
moderator to leave Coop to find out who they are deciding about. These fields put
the person on the card: display name, nip05 and its verification state, follower
counts, and whether the account has asked to vanish.

**The resolution STATE is a field, not an inference from blankness.** funnelcake
answers `GET /api/users/{pubkey}` with HTTP 200 and a null profile for a pubkey it
has never seen, so a blank name means either "this account published no profile"
(a fact about the user, safe to act on) or "we could not look it up" (a fact about
our infrastructure, meaning the card is missing evidence). Status code cannot tell
them apart. Carrying the state explicitly is the whole point of this module; see
test_coop_profile.py.

Deliberately pure: the HTTP call, its timeout and its failure handling live in the
sink, and the error string is passed in. Same split as `coop_payload.py`, and for
the same reason -- the plugin test step installs pytest and websocket-client only.
"""

from typing import Any

RESOLVED = 'resolved'
NO_PROFILE = 'no_profile'
LOOKUP_FAILED = 'lookup_failed'


def profile_fields(
    response: dict[str, Any] | None,
    *,
    prefix: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the `<prefix>_*` profile fields for one pubkey.

    Args:
        response: funnelcake's `GET /api/users/{pubkey}` body, or None if the call
            did not produce one. A body with `profile: null` is a successful lookup
            of a user who has published no profile -- NOT a failure.
        prefix: `author`, `reported` or `reporter`. These are three different people
            answering three different questions, so each gets its own namespace; one
            shared set of fields would show one person's identity against another's
            actions.
        error: why the lookup failed, when it did. Surfaced verbatim so the failure
            is actionable rather than merely visible.

    Returns:
        Fields to merge into the Coop item. Always includes `<prefix>_profile_state`.
        Empty values are omitted rather than sent blank, matching
        `build_content_fields` -- Coop renders every field it is given, so a blank
        row is noise on the card.
    """
    out: dict[str, Any] = {}

    if response is None:
        out[f'{prefix}_profile_state'] = LOOKUP_FAILED
        if error:
            out[f'{prefix}_profile_error'] = error
        return out

    # Checked BEFORE the profile branch, on purpose. A vanish request is a property
    # of the ACCOUNT, not of the profile: a user who published no kind-0 can still
    # have asked for their data gone, and that is arguably the more urgent case. An
    # earlier version returned early on a missing profile and silently dropped it.
    if response.get('has_vanish_request'):
        out[f'{prefix}_has_vanish_request'] = True

    profile = response.get('profile')
    if not profile:
        out[f'{prefix}_profile_state'] = NO_PROFILE
        return out

    out[f'{prefix}_profile_state'] = RESOLVED

    # display_name is the NIP-24 preference; `name` is the NIP-01 fallback. A profile
    # can carry one, both or neither, and the card must still name someone.
    display = profile.get('display_name') or profile.get('name')
    if display:
        out[f'{prefix}_display_name'] = display

    if profile.get('nip05'):
        verified = bool(profile.get('nip05_verified'))
        # The status is folded INTO the string, deliberately. Coop has two renderers:
        # the review card shows every declared field, but the queue preview uses
        # getPrimaryContentFields, which filters to STRING/URL/media and drops
        # BOOLEAN (client/src/utils/itemUtils.ts:112-118). A separate boolean would
        # be invisible exactly where a moderator scans fastest -- they would read a
        # bare 'sam@divine.video' as an established identity when it is an unverified
        # claim anyone can make. A single string cannot be separated from itself.
        out[f'{prefix}_nip05'] = f'{profile["nip05"]} ({"verified" if verified else "UNVERIFIED"})'
        # Kept alongside for machine use (rules, exports). Sent even when False, so
        # absent means "no nip05 claimed" rather than "claimed but unverified".
        out[f'{prefix}_nip05_verified'] = verified

    social = response.get('social') or {}
    if social.get('follower_count') is not None:
        out[f'{prefix}_follower_count'] = social['follower_count']

    # Only when true. A vanish request changes what a moderator should do, so it is
    # worth a row; its absence is the normal case and does not need one.
    if response.get('has_vanish_request'):
        out[f'{prefix}_has_vanish_request'] = True

    return out
