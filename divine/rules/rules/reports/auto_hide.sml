# Trusted Reporter Auto-Hide
# Automatically acts on reports from trusted reporters for CSAM and NSFW content.
#
# Report reasons are normalized by the bridge. Canonical values:
#   csam, nudity, violence, ai_generated, spam, impersonation, illegal,
#   harassment, other
#
# Mobile sends 'illegal' for CSAM (NIP-56 mapping), which the bridge
# can't distinguish from violence/copyright 'illegal'. We match both
# 'csam' (unambiguous) and 'illegal' (may over-match, but for trusted
# reporters the cost of a false auto-hide on 'illegal' is low).

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

TrustedReporterCSAM = Rule(
  when_all=[
    Kind == 1984,
    HasLabel(entity=Pubkey, label='trusted_reporter'),
    ReportReason in ['csam', 'illegal'],
  ],
  description='Trusted reporter flagged CSAM or illegal content',
)

TrustedReporterNSFW = Rule(
  when_all=[
    Kind == 1984,
    HasLabel(entity=Pubkey, label='trusted_reporter'),
    ReportReason == 'nudity',
  ],
  description='Trusted reporter flagged NSFW content',
)

# Event-level ban only. A reporter-supplied p-tag can name any pubkey, so
# ReportedPubkey is an unverified claim and is not safe to enforce against --
# the same reasoning multi_report_threshold.sml states and follows, and the same
# thing relay-manager's ReportWatcher does (it bans the event and never the
# author). Banning the event is safe because the event id is content-addressed:
# naming it is proving it. The author decision goes to a human instead, via the
# auto_hide verdict landing in COOP's CSAM queue.
WhenRules(
  rules_any=[TrustedReporterCSAM],
  then=[
    BanNostrEvent(event_id=ReportedEventId, pubkey='', reason='CSAM reported by trusted reporter'),
    # Persist the enforcement state so a later ordinary report does not hide the
    # same event or publish a second audit label.
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)

WhenRules(
  rules_any=[TrustedReporterNSFW],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
    LabelAdd(entity=ReportedPubkey, label='nsfw_flagged', expires_after=TimeDelta(days=7)),
  ],
)
