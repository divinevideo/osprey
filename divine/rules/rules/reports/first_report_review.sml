# First-Report Review (Child Safety, Harassment, CSAM, Illegal, Spam, Impersonation, Other)
#
# Flags a report for HUMAN REVIEW on the FIRST report of an event, from ANY reporter
# (NOT gated to trusted reporters). Every category here emits flag_for_review only --
# a moderator triages in the matching Coop queue (Child Safety, Harassment, CSAM,
# General Review); no automatic enforcement. Modeled on FirstSexualReport in
# multi_report_threshold.sml.
#
# Together with nudity and violence (multi_report_threshold.sml), this covers all nine
# canonical report reasons the bridge emits. Before csam, illegal, spam, impersonation
# and other were added here, an ordinary user reporting any of them reached no moderator
# through Osprey at all.
#
# WHY NOT trusted-reporter-gated: "trusted reporter" today is a hardcoded list of two
# system identities (admin + the moderation-service), seeded manually
# (divine/scripts/seed-trusted-reporters.sh). Gating these categories on it would mean
# ordinary users' child-safety / harassment reports never surface via Osprey. nudity
# and violence already surface on any user's first report; these match that, since the
# only action is flag_for_review (a human still decides). The trusted-reporter gate is
# reserved for AUTOMATIC actions (the CSAM/NSFW auto-hide in auto_hide.sml). An evolved
# trust model is roadmapped (support-trust-safety/docs/moderation/coop-osprey-future-lifts-roadmap.md).
#
# The <reason>_reported label dedups: the first report flags + labels the event, so a
# second report of the same category on the same event does not re-flag. human_reviewed
# suppresses re-flagging content a moderator has already handled.
#
# LIMITATION: keyed on ReportedEventId, so this only covers content (event) reports, not
# user-only reports (a p-tag with no e-tag) -- same as the existing nudity/violence
# rules. User-targeted reports (e.g. underage accounts) go via the relay-manager path.
#
# Report reasons are normalized by the bridge (childSafety -> child_safety, etc.).
# NB each Rule name below becomes a ClickHouse column (clickhouse-schema/001_osprey_events.sql).

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

FirstChildSafetyReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'child_safety',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='child_safety_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of a child-safety concern on this event',
)

FirstHarassmentReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'harassment',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='harassment_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of harassment on this event',
)

# csam and illegal reach a moderator on ANY user's first report. auto_hide.sml also
# matches these two, but only from a trusted reporter, and a trusted reporter today is
# two seeded system identities -- so an ordinary user reporting CSAM produced NOTHING in
# Coop at all (verified 2026-08-14: a clean single-report probe created no Coop item,
# and the CSAM queue sat empty). The report was reaching Osprey and being parsed
# correctly; there was simply no rule that matched it.
#
# flag_for_review only, deliberately: automatic enforcement stays gated on trust, per
# this file's rule above. Auto-hiding on any single csam report would let one account
# mass-report and hide arbitrary content. The immediate auto-hide for real in-app
# reports continues to come from relay-manager's ReportWatcher, whose gate is a trusted
# `client` tag rather than reporter reputation -- a signal the bridge does not currently
# expose to Osprey. Exposing it is what would let Osprey take over that half.
FirstCsamReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'csam',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='csam_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First CSAM report on this event, from any reporter',
)

FirstIllegalReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'illegal',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='illegal_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of illegal content on this event, from any reporter',
)

# spam, impersonation and other matched NO rule at all, so they never reached a
# moderator through Osprey. They route to Coop's General Review queue.
FirstSpamReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'spam',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='spam_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First spam report on this event',
)

FirstImpersonationReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'impersonation',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='impersonation_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First impersonation report on this event',
)

FirstOtherReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'other',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='other_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First uncategorised report on this event',
)

WhenRules(
  rules_any=[FirstChildSafetyReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='child_safety_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstCsamReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='csam_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstIllegalReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='illegal_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstSpamReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='spam_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstImpersonationReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='impersonation_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstOtherReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='other_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstHarassmentReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='harassment_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
