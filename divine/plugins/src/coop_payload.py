"""Turns Osprey features into the `content` fields Coop stores on an item.

Lifted out of `COOPSink._submit_content` **without behaviour change**, so that
what we send to moderators can be asserted on directly. `coop_sink.py` imports
gevent, requests, sentry_sdk and the osprey engine, while the plugin test step
installs pytest and websocket-client only -- the same reason `media_hash.py`,
`reported_author.py` and `trusted_moderation.py` are separate modules.

Those imports make the sink awkward to test, not impossible: stubbing them into
`sys.modules` works, and `test_coop_sink_payload_wire.py` does it to assert on the
actual POST body. Both layers are wanted. This one pins field-by-field behaviour
cheaply; that one catches whether the right values reach the call at all.

Deliberately pure: no I/O, no clock, no config beyond what is passed in. The media
lookup that runs afterwards still lives in the sink and mutates the returned dict,
because it is a network call with its own timeout and fail-open semantics.

The field set here is what a moderator reads on the review card, so a change to it
is a change to what a human sees when deciding whether to remove someone's
content. test_coop_payload.py pins the current shape for exactly that reason.
"""

from typing import Any

from reported_author import _hex64

# Detector Actions are keyed on a SHA-256 computed from fetched bytes rather than
# on a Nostr event id, so they are the one case where this function can build a
# playable URL itself.
DETECTOR_ACTION = 'ai_detector_nsfw'


def build_content_fields(
    features: dict[str, Any],
    *,
    content_id: str,
    wrapper_event_id: str,
    author: str,
    verdict: str,
    action_name: str,
    media_base_url: str,
    user_type_id: str = '',
) -> dict[str, Any]:
    """Build the `content` object for a Coop item submission.

    Args:
        features: Osprey's extracted features for the action.
        content_id: Id of the MODERATED event -- the offending content, not the
            report or label that wrapped it.
        wrapper_event_id: Id of the event that carried the moderation signal.
        author: Who signed the moderated content. Becomes Coop's `creator` and
            therefore the subject of Unban-User / Unsuspend-User. **`''` when it
            could not be resolved**, which is carried through in `pubkey` rather than
            dropped so the adapter refuses loudly instead of acting on a guess. The
            `author` RELATED_ITEM, by contrast, is omitted unless this is 64-char hex,
            because a junk id there becomes an actionable account rather than an error.
        verdict: The verdict string.
        action_name: Osprey action name; selects the detector branch below.
        media_base_url: Trusted base for detector media URLs.
        user_type_id: Coop's `nostr_user` item type id, from DIVINE_COOP_USER_TYPE_ID.
            Empty when iac has not plumbed it yet, in which case the `author` field is
            omitted rather than guessed.

    Returns:
        The content fields. The caller may add `media_url` / `media_thumbnail`
        afterwards from a relay lookup.
    """
    author_id = _hex64(author)
    content: dict[str, Any] = {
        'event_id': content_id,
        'source_event_id': wrapper_event_id,
        # Describes event_id, not source_event_id. The wrapper's own signer is
        # not carried here; `reported_pubkey` below keeps the reporter's
        # unverified claim, clearly labelled as a claim.
        # Keep every account identifier on the canonical spelling when the
        # input is a valid pubkey. Invalid legacy values remain unchanged so
        # this field preserves its existing fail-loud behaviour.
        'pubkey': author_id or author,
        'kind': features.get('Kind'),
        'created_at': features.get('CreatedAt'),
        'verdict': verdict,
        'action_name': action_name,
    }

    # Truthiness, not presence: an empty value is omitted entirely rather than
    # sent as ''. Coop renders every field it is given, so a blank row would be
    # noise on the moderator's card.
    if features.get('ReportReason'):
        content['report_reason'] = features['ReportReason']
    if features.get('ReportedPubkey'):
        content['reported_pubkey'] = features['ReportedPubkey']
    if features.get('ReportedEventId'):
        # str() alone among these: it arrives from a report's e-tag and is not
        # guaranteed to be a string.
        content['reported_event_id'] = str(features['ReportedEventId'])
    if features.get('LabelValue'):
        content['label_value'] = features['LabelValue']
    if features.get('LabelNamespace'):
        content['label_namespace'] = features['LabelNamespace']
    if features.get('NoteText'):
        content['text'] = features['NoteText']

    # Coop's `author` is a RELATED_ITEM and `creatorId` points at it. Without it the
    # Associated User panel does not render, so Ban/Suspend/Unban/Unsuspend-User cannot be
    # exercised at all -- half of moderation, silently missing. `pubkey` above cannot carry
    # the role: it is STRING-typed, and Coop rejects a STRING field in a RELATED_ITEM role.
    #
    # EMIT OR OMIT, never a placeholder, and on SHAPE rather than truthiness. Coop 400s the
    # WHOLE submission when a RELATED_ITEM id is empty, but ACCEPTS any id of length > 0 --
    # so junk is not an error, it silently creates a related user item a moderator can then
    # Ban. `author` is hex64-validated on the 1984/1985 paths, but non-wrapper actions pass
    # `Pubkey` through unvalidated. Losing one item's account panel is recoverable; losing
    # the item, or actioning a fabricated account, is not.
    #
    # Emit the NORMALIZED id: _hex64 strips and lowercases, and an uppercase pubkey left raw
    # would be a SECOND, distinct related item in Coop for the very same account.
    if author_id and user_type_id:
        content['author'] = {'id': author_id, 'typeId': user_type_id}

    # Build the playable URL from the validated identity and a trusted base;
    # never pass through the caller-controlled URL carried for diagnostics in the
    # Action. Requires BOTH the detector action AND the hash naming this very
    # item, so a detector result about different bytes cannot attach a URL here.
    if action_name == DETECTOR_ACTION and features.get('DetectorContentHash') == content_id:
        content['media_url'] = f'{media_base_url}/{content_id}'
        content['label_namespace'] = 'content-warning'
        content['label_value'] = features.get('DetectorClass', 'nsfw')
        content['confidence'] = features.get('DetectorConfidence', 0)
        content['model'] = features.get('DetectorModel', '')

    return content
