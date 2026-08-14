"""A moderator must see a person, not 64 hex characters -- and must be able to tell
'this account published no profile' apart from 'we could not look it up'.

Those two are the same blank space on a review card, and they mean opposite things.
The first is a fact about the user and is safe to act on. The second is a fact about
our infrastructure and means the card is missing evidence the moderator was entitled
to. funnelcake makes this trap easy: `GET /api/users/{pubkey}` answers **HTTP 200
with a null profile** for a pubkey it has never seen, so status code alone cannot
separate them. Measured 2026-08-14 against a local funnelcake:

    unknown pubkey -> HTTP 200  {"profile": null, "social": {...}, ...}

That is the same silent-success shape as the Cloudflare Access auth page (a 200
that is not an answer) and funnelcake's own rate limiter (a 500 nothing surfaced).
So the resolution STATE is carried explicitly as a field, and these tests exist to
keep it that way.
"""

import pytest
from coop_profile import profile_fields

PUBKEY = '46e8133b46fc90dc9b729d561bb26442acc62c38d61b4a5cab351de61366b83b'


def test_a_resolved_profile_gives_the_moderator_a_name():
    got = profile_fields(
        {
            'pubkey': PUBKEY,
            'profile': {'display_name': 'Six Second Sam', 'nip05': 'sam@divine.video', 'nip05_verified': True},
            'social': {'follower_count': 1234, 'following_count': 12},
            'has_vanish_request': False,
        },
        prefix='author',
    )
    assert got['author_display_name'] == 'Six Second Sam'
    assert got['author_nip05'] == 'sam@divine.video (verified)'
    assert got['author_profile_state'] == 'resolved'


def test_no_profile_and_lookup_failed_are_DIFFERENT_states():
    """The assertion this module exists for. Both leave the name blank; only one
    means the card is untrustworthy."""
    no_profile = profile_fields({'pubkey': PUBKEY, 'profile': None}, prefix='author')
    failed = profile_fields(None, prefix='author', error='timeout')

    assert no_profile['author_profile_state'] == 'no_profile'
    assert failed['author_profile_state'] == 'lookup_failed'
    assert no_profile['author_profile_state'] != failed['author_profile_state']


def test_a_failed_lookup_says_WHY_so_it_is_actionable():
    got = profile_fields(None, prefix='author', error='HTTP 500 Unable To Extract Key!')
    assert 'Unable To Extract Key' in got['author_profile_error']


def test_an_unverified_nip05_is_not_presented_as_verified():
    """A nip05 is a self-asserted claim until funnelcake verifies it. Showing an
    unverified one indistinguishably from a verified one lets an impersonator borrow
    a trusted identity on the moderator's own screen."""
    got = profile_fields(
        {
            'pubkey': PUBKEY,
            'profile': {'display_name': 'Not Really Sam', 'nip05': 'sam@divine.video', 'nip05_verified': False},
        },
        prefix='author',
    )
    # The STATUS TRAVELS INSIDE THE STRING, and that is not cosmetic. Coop has two
    # renderers: the review card shows every declared field, but the queue preview
    # uses getPrimaryContentFields, which filters to STRING/URL/media and DROPS
    # BOOLEAN. A separate boolean flag would therefore be invisible exactly where a
    # moderator scans quickly -- they would see a bare 'sam@divine.video' with no
    # hint it is an unverified claim. One field cannot be separated from itself.
    assert got['author_nip05'] == 'sam@divine.video (UNVERIFIED)'
    assert got['author_nip05_verified'] is False


def test_a_verified_nip05_is_marked_verified_in_the_string_too():
    got = profile_fields(
        {'pubkey': PUBKEY, 'profile': {'nip05': 'sam@divine.video', 'nip05_verified': True}},
        prefix='author',
    )
    assert got['author_nip05'] == 'sam@divine.video (verified)'
    assert got['author_nip05_verified'] is True


@pytest.mark.parametrize('prefix', ['author', 'reported', 'reporter'])
def test_all_three_pubkeys_get_their_own_namespaced_fields(prefix):
    """Author, reported and reporter are three different people and three different
    questions. Reusing one set of fields for all of them would silently show one
    person's identity against another's actions."""
    got = profile_fields({'pubkey': PUBKEY, 'profile': {'display_name': 'X'}}, prefix=prefix)
    assert f'{prefix}_display_name' in got
    assert f'{prefix}_profile_state' in got
    assert not any(k.startswith('author_') for k in got if prefix != 'author')


def test_a_missing_display_name_falls_back_to_something_human():
    """A profile can exist with no display_name. The card must still say who this is
    rather than going blank, and `name` is the NIP-01 fallback."""
    got = profile_fields({'pubkey': PUBKEY, 'profile': {'name': 'sam', 'display_name': None}}, prefix='author')
    assert got['author_display_name'] == 'sam'
    assert got['author_profile_state'] == 'resolved'


def test_a_vanish_request_is_surfaced_because_it_changes_the_decision():
    """has_vanish_request means the user asked for their data gone. A moderator
    acting on that account needs to know before, not after."""
    got = profile_fields({'pubkey': PUBKEY, 'profile': {}, 'has_vanish_request': True}, prefix='author')
    assert got['author_has_vanish_request'] is True


def test_empty_values_are_omitted_rather_than_sent_blank():
    """Matches build_content_fields: Coop renders every field it is given, so a blank
    row is noise on the card."""
    got = profile_fields({'pubkey': PUBKEY, 'profile': {'display_name': ''}}, prefix='author')
    assert 'author_display_name' not in got
