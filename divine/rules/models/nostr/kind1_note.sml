# UNREACHABLE from main.sml as of 2026-08-16. Its only importer was
# behavioral/new_account_spam.sml, which was retired. Kept rather than deleted,
# for two reasons:
#
#   1. DO NOT DELETE WITHOUT A PAIRED iac CHANGE. The osprey-workers container
#      command runs, under `set -e`:
#        sed -i 's/MentionedPubkeys: list/MentionedPubkeys: List[str]/' \
#          /osprey/divine_rules/models/nostr/kind1_note.sml
#      `sed -i` on a missing file exits non-zero, so removing this file kills the
#      container before it reaches `exec` and the workers crashloop. Verified
#      2026-08-16. Removing it means editing divine-iac-coreconfig
#      k8s/applications/osprey-workers/base/deployment.yaml in the same change.
#   2. It is the model a future kind-1 rule would need, and nothing here is wrong.

Import(rules=['models/base.sml'])

NoteText: str = JsonData(
  path='$.content',
  coerce_type=True
)

MentionedPubkeys: List[str] = JsonData(
  path='$.mentioned_pubkeys',
  coerce_type=True,
  required=False
)
