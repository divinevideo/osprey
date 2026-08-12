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

# One entity per (reported event, reporter) pair.
#
# Osprey has no counting primitive, so a threshold is emulated as a chain of
# label presences on the target. That chain cannot tell two reporters from one
# reporter reporting twice, which multi_report_threshold.sml documents as its P2
# defect. Labelling this composite entity instead makes each reporter's
# contribution idempotent: a second report from the SAME reporter finds the
# label already set and does not advance the threshold.
#
# This is distinct-reporter DEDUP, not counting. The threshold is still fixed at
# 2 by the shape of the rules. A real count needs the funnelcake accessor in the
# PRD's C2 (`uniq(reporter_pubkey)` already exists there).
EventReporterId: Entity[str] = Entity(
  type='EventReporter',
  id=StringJoin(s=':', iterable=[ReportedEvent, ReporterPubkeyStr]),
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

ReportReason: str = JsonData(
  path='$.report_reason',
  coerce_type=True,
  required=False
)
