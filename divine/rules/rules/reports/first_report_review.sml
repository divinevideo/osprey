# First-Report Review (Child Safety, Harassment, CSAM, Illegal, Spam, Impersonation, AI-Generated, Other)
#
# Flags a report for HUMAN REVIEW on the FIRST report of an event, from ANY reporter
# (NOT gated to trusted reporters). Every category here emits flag_for_review EXCEPT
# csam, which additionally hides the event -- see the CSAM block below for why that one
# acts without a trust gate. A moderator triages in the matching Coop queue (Child
# Safety, Harassment, CSAM, General Review). Modeled on FirstSexualReport in
# multi_report_threshold.sml.
#
# The bridge catalogues ELEVEN canonical reasons, but it does not constrain the token
# it emits: an unrecognised reason passes through unchanged. With nudity and violence
# (multi_report_threshold.sml), this file covers every reason a user can report --
# named or not -- except `underage_user`, which is deliberately relay-manager's: it
# feeds the age-review case system (15-day clock, age tiers, suspension), not a Coop
# queue. FirstOtherReport is what makes that "every": it matches by exclusion, so a
# reason no rule names is still seen by a human. See the block above it.
#
# Before csam, illegal, spam, impersonation, other and ai_generated were added here, an
# ordinary user reporting any of them reached no moderator through Osprey at all.
# ai_generated was the subtlest: it was matched only by moderation_service.sml, and
# only for a service-signed report, so a USER reporting AI slop was silently dropped.
# That rule was removed on 2026-08-16, so this file is now the only path for it.
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

# CSAM and illegal reach a moderator on ANY user's first report. Trusted illegal
# reports also have an auto-hide path in auto_hide.sml, while FirstCsamReport is
# the single CSAM path for every reporter. Before this rule, an ordinary user
# reporting CSAM produced NOTHING in Coop at all (verified 2026-08-14: a clean
# single-report probe created no Coop item, and the CSAM queue sat empty).
#
# illegal stays flag_for_review: it over-matches (auto_hide.sml says so), so acting on
# one unverified report would hide too much. csam does act -- see the CSAM WhenRules
# block below for the reasoning and the reversibility that makes it safe.
FirstCsamReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'csam',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='csam_reported'),
  ],
  description='First CSAM report on this event, from any reporter',
)

FirstIllegalReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'illegal',
    ReportedEvent != '',
    # auto_hide.sml has a stronger trusted illegal path.
    not HasLabel(entity=Pubkey, label='trusted_reporter'),
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

# ai_generated was matched ONLY by moderation_service.sml, which required the signer to
# carry the `moderation_service` label. divine-web sends 'ai-generated' and divine-mobile
# sends 'aiGenerated', so ordinary users do report it -- and those reports reached nobody.
#
# That rule was removed on 2026-08-16 and nothing writes `moderation_service`, so the
# signer guard below is inert today. It is retained deliberately: it costs nothing, and
# it is the correct guard again the moment a service-signed 1984 path exists.
FirstAiGeneratedReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'ai_generated',
    ReportedEvent != '',
    not HasLabel(entity=Pubkey, label='moderation_service'),
    not HasLabel(entity=ReportedEventId, label='ai_generated_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First AI-generated report on this event, from an ordinary reporter',
)

# The CATCH-ALL. Matches by exclusion, so a reason no other rule names still
# reaches a moderator instead of vanishing.
#
# It has to be a negation rather than a list of known reasons. A report whose
# reason matches nothing declares no verdict, so COOPSink.will_do_work returns
# false and nothing is submitted -- the report lands in ClickHouse and no
# moderator ever sees it. `copyright` (divine-web, live today) and `hate`
# (divine-mobile #7636) are in exactly that state, and every reason a client
# adds in future would join them. Matching by exclusion makes the default
# "a human looks at it" rather than "silently dropped".
#
# The list below is every reason another owner already handles, and it is
# exact in both directions: excluding a reason nothing else owns re-opens the
# silent drop, and failing to exclude one owned elsewhere makes two rules fire
# on one report and produce a duplicate Coop item. Nine come from the other
# report rules (this file, auto_hide.sml, multi_report_threshold.sml);
# `underage_user` is relay-manager's age-review case system, not Osprey's, so
# Osprey must not queue it. divine/rules/tests/test_report_reason_catchall.py
# derives both halves from the live rules and the bridge's CANONICAL_REASONS
# table and fails if this list drifts from either.
#
# Dedup is unchanged in shape but now wider in reach: `other_reported` is one
# label for the whole uncategorised family, so a copyright report followed by a
# hate report on the SAME event flags once, not twice. That matches how the
# rule already treated two 'other' reports, and one queue item per event is the
# intent; distinguishing them needs its own reason, rule and ClickHouse column.
#
# A report with NO reason also lands here. The bridge only sets report_reason
# when it can extract one, and the engine evaluates an absent value as
# not-in-the-list, so a 1984 about a specific event whose reason could not be
# parsed still queues for review: a user said something about that event, and
# the same default applies. Verified in the real engine 2026-08-17. A p-tag-only
# report (no e-tag) does NOT reach this rule: ReportedEventId above is a
# required EntityJson, and its missing path fails the rule before any verdict.
FirstOtherReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason not in [
      'csam',
      'illegal',
      'child_safety',
      'harassment',
      'nudity',
      'violence',
      'ai_generated',
      'spam',
      'impersonation',
      'underage_user',
    ],
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='other_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report on this event whose reason no other rule handles',
)

WhenRules(
  rules_any=[FirstChildSafetyReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='child_safety_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# CSAM is the one category that ACTS on a single report from ANY reporter, because the
# cost of a missed CSAM report is not comparable to the cost of a wrongly hidden post.
#
# Before this, a csam report that did not carry a trusted `client` tag was caught by
# NOTHING: ReportWatcher's immediate tier is `requireTrustedClient: true` (its default
# config, trustedClients = diVine,divine-web,divine-mobile), so it skipped the report
# entirely, and Osprey had no csam rule to fall back on. Verified 2026-08-14.
#
# `csam_reported` is the category-specific dedup. A broad `human_reviewed` guard
# would silently discard a new CSAM allegation after an unrelated human decision.
# What makes acting on an unverified report acceptable here is that the action is
# EVENT-LEVEL and reversible: `pubkey=''` means RelayManagerSink issues banevent, never
# banpubkey, so nothing is purged and no account is touched (see
# divine/rules/tests/test_enforcement_targets.py, which enforces that boundary). A
# moderator reverses it from the CSAM queue with Restore-Content. The account decision
# stays with the human.
#
# The bound is one hide PER EVENT, via csam_reported -- it is NOT a bound per actor.
# One account reporting N different events causes N hides and N items in this queue,
# and nothing here dedups by reporter (multi_report_threshold.sml carries a
# `reporter_counted` guard; this path has no equivalent because it has no count).
# Queue flooding is the sharper risk, not the hides: burying real reports under sybil
# noise degrades the NCMEC-bound queue this rule exists to feed. The mitigation is a
# real trust signal rather than a weaker action -- distinct-reporter counting and/or
# the `client` tag, both roadmapped. Osprey has to own that: Coop cannot count for us
# (AGGREGATION signals are hardcoded to two test org ids in its image).
WhenRules(
  rules_any=[FirstCsamReport],
  then=[
    BanNostrEvent(event_id=ReportedEventId, pubkey='', reason='CSAM reported; hidden pending human review'),
    LabelAdd(entity=ReportedEventId, label='csam_reported'),
    DeclareVerdict(verdict='auto_hide'),
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
  rules_any=[FirstAiGeneratedReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='ai_generated_reported'),
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
