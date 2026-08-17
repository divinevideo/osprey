# Profile-only report review.
#
# A NIP-56 report can name an ACCOUNT (a `p` tag) with NO content event (no `e`
# tag). Every content report rule -- first_report_review.sml,
# multi_report_threshold.sml, auto_hide.sml -- guards on ReportedEvent != '', so a
# profile-only report matched nothing, declared no verdict, and reached no
# moderator through Osprey at all. This is the one rule that fires on that shape.
#
# It emits flag_for_review ONLY, never an action on the account. There is no event
# to hide, and hiding an account is banpubkey, which irreversibly PURGES it. Humans
# decide accounts (support-trust-safety reference_osprey_enforcement_boundary), so
# even a profile-only csam report routes to the CSAM queue for a human rather than
# acting. That is the deliberate asymmetry with FirstCsamReport, which auto-hides an
# EVENT because an event ban is reversible.
#
# underage_user is excluded: age review is relay-manager's case system (15-day
# clock, age tiers, suspension), fed by the direct bridge import and moderators
# recategorizing, NOT the live Osprey path. This is the same carve-out
# first_report_review.sml's catch-all makes for content.
#
# COOPSink submits the reported account (ReportedPubkey) as a nostr_user item
# carrying report_reason; coop-setup-org.sh routes nostr_user items by report_reason
# into the existing reason queues. See
# support-trust-safety/docs/superpowers/specs/2026-08-17-profile-reports-as-coop-items-design.md.
#
# Dedup labels the reported PUBKEY, not an event -- there is no event. A second
# report of the same account finds `user_reported` set and does not re-flag, so
# one queue item per account, matching how content dedups one item per event.
# human_reviewed suppresses re-flagging an account a moderator already handled.
#
# NB the Rule name becomes a ClickHouse column
# (clickhouse-schema/001_osprey_events.sql). iac must add
# `FirstUserReport UInt8 DEFAULT 0` before this deploys, or a missing column fails
# the entire batch insert.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

FirstUserReport = Rule(
  when_all=[
    Kind == 1984,
    ReportedPubkeyStr != '',
    ReportedEvent == '',
    ReportReason != 'underage_user',
    not HasLabel(entity=ReportedPubkey, label='user_reported'),
    not HasLabel(entity=ReportedPubkey, label='human_reviewed'),
  ],
  description='First report of an account with no content event (profile-only report)',
)

WhenRules(
  rules_any=[FirstUserReport],
  then=[
    LabelAdd(entity=ReportedPubkey, label='user_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
