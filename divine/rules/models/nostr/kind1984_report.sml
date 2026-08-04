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
