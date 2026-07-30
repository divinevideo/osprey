# First-Report Review (Child Safety, Harassment, CSAM)
#
# Flags child_safety, harassment and csam reports for HUMAN REVIEW on the FIRST report
# of an event, from ANY reporter (NOT gated to trusted reporters). These categories only
# emit flag_for_review -- a moderator triages in the dedicated Coop queue (Child
# Safety, Harassment, CSAM); no automatic enforcement. Modeled on FirstSexualReport in
# multi_report_threshold.sml.
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

# CSAM from ANY reporter. Before this, csam was matched only by TrustedReporterCSAM
# (auto_hide.sml), so a report from an ordinary user produced no verdict at all and
# COOPSink submitted nothing -- csam was the only severe reason with no ordinary-reporter
# path, and Coop's CSAM queue could not fill from user reports. Enforcement stays with
# the trusted-reporter rule; this only routes a human to it.
#
# Matches 'csam' ONLY, not the ['csam','illegal'] pair auto_hide.sml uses. divine-mobile
# maps several reasons onto the NIP-56 'illegal' type (csam, violence, copyright), so
# 'illegal' alone is ambiguous and would pull violence and copyright reports into the CSAM
# queue. It is safe to match 'csam' alone because the bridge reads the NIP-32 'l' tag
# (priority 2, 'NS-csam' -> 'csam') ahead of the e-tag's report type (priority 4), and
# both mobile and web send that label.
#
# NO human_reviewed guard, unlike its siblings. That is deliberate, per
# support-trust-safety docs/moderation/csam-sticky-status-design.md §3.4: a prior human
# decision must stand, but a CSAM report on already-reviewed content must still reach a
# human in the CSAM queue rather than being silently dropped. Because this rule declares
# only flag_for_review and takes no enforcement action, surfacing it re-opens review
# without undoing the earlier decision.
#
# KNOWN LIMITATION -- csam_reported is a ONE-SHOT, PERMANENT gate. It never expires,
# nothing removes it, and there is no second-stage rule for csam (contrast
# ThresholdSexualReport, which fires *because* sexual_reported exists). So after the
# first csam report on an event, this rule can never fire for that event again, whether
# or not a human ever actually reviewed it. COOP decisions do not write back into Osprey
# -- the webhook adapter only calls relay-manager RPC and publishes no kind-1985 labels
# -- so a moderator dismissing the job clears nothing and records nothing here.
#
# The consequence is a denial-of-moderation vector this file's other categories share
# but which matters far more for csam: one signed kind-1984 permanently consumes an
# event's only ordinary-reporter CSAM review slot. Dropping the human_reviewed guard does
# not cause this and does not fix it; csam_reported is the operative gate either way.
# Resolving it needs one of: an expiry on the label, a second-stage rule keyed on the
# label, or the COOP-to-Osprey decision feedback path that does not exist yet.
# Tracked against support-trust-safety#154.
FirstCSAMReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'csam',
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='csam_reported'),
  ],
  description='First CSAM report on this event, from any reporter',
)

WhenRules(
  rules_any=[FirstChildSafetyReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='child_safety_reported'),
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

WhenRules(
  rules_any=[FirstCSAMReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='csam_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
