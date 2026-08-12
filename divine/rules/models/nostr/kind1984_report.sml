Import(rules=['models/base.sml'])

ReportedEventId: Entity[str] = EntityJson(
  type='ReportedEventId',
  path='$.reported_event_id',
  coerce_type=True
)

# Plain string version for UDF arguments (BanNostrEvent, AgeRestrictNostrEvent).
# Entity version above is for LabelAdd/HasLabel.
ReportedEvent: str = JsonData(
  path='$.reported_event_id',
  coerce_type=True,
  required=False
)

# The reporter's own pubkey as a plain string. `Pubkey` in base.sml is the
# Entity form; this is for composing the key below.
ReporterPubkeyStr: str = JsonData(
  path='$.pubkey',
  coerce_type=True,
)

# The report's category, normalised by the bridge to a canonical value. Declared
# here rather than further down because the composite key below depends on it.
ReportReason: str = JsonData(
  path='$.report_reason',
  coerce_type=True,
  required=False
)

# One entity per (reported event, report category, reporter) triple.
#
# Osprey has no counting primitive, so a threshold is emulated as a chain of
# label presences on the target. That chain cannot tell two reporters from one
# reporter reporting twice, which multi_report_threshold.sml documents as its P2
# defect. Labelling this composite entity instead makes each reporter's
# contribution idempotent: a second report from the SAME reporter finds the
# label already set and does not advance the threshold.
#
# The CATEGORY is part of the key, and leaving it out was a silent-drop bug.
# A single `reporter_counted` label guards every category's rules at once, so
# without the category one report poisons the rest: a reporter who flags an event
# for violence and later flags the same event for nudity matches neither the
# first-report rule (guard already set) nor the threshold rule (that category has
# no prior report), so the second report matches NOTHING. No verdict, no COOP
# item, no telemetry. It also inflated the threshold, since that category then
# needed a third distinct reporter.
#
# The dedup that is wanted is "this person has already made THIS accusation",
# not "this person has already spoken about this event".
#
# Ordering is deliberate. The reason is drawn from the bridge's canonical set and
# the reporter pubkey is fixed-length hex from the signed event, so both are
# colon-free and the id parses unambiguously from the right. Only the event id is
# attacker-shaped, and it sits first where it cannot absorb a neighbouring field.
#
# This is distinct-reporter DEDUP, not counting. The threshold is still fixed at
# 2 by the shape of the rules. A real count needs the funnelcake accessor in the
# PRD's C2 (`uniq(reporter_pubkey)` already exists there).
EventReporterId: Entity[str] = Entity(
  type='EventReporter',
  id=StringJoin(s=':', iterable=[ReportedEvent, ReportReason, ReporterPubkeyStr]),
)

# CLAIMED author. Written by the reporter in the report's p-tag and never
# checked against the reported event. Do not enforce on this; see
# ReportedAuthorPubkey below.
ReportedPubkey: Entity[str] = EntityJson(
  type='ReportedPubkey',
  path='$.reported_pubkey',
  coerce_type=True
)

# AUTHORITATIVE author, resolved from the reported event itself. An attacker
# chooses which event to report but cannot change who signed it.
#
# '' whenever it cannot be trusted: no e-tag, event not found, the relay API
# unreachable, or a response whose id does not match the request. Consumers must
# treat '' as "no authoritative author" and decline to enforce, never falling
# back to ReportedPubkey.
#
# Costs nothing on non-report events: a missing or malformed event id returns ''
# without any network call.
ReportedAuthorPubkey: str = ResolveEventAuthor(event_id=ReportedEvent)
