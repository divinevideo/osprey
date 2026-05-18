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

ReportedPubkey: Entity[str] = EntityJson(
  type='ReportedPubkey',
  path='$.reported_pubkey',
  coerce_type=True
)

ReportReason: str = JsonData(
  path='$.report_reason',
  coerce_type=True,
  required=False
)
