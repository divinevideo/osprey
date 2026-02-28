Import(rules=['models/base.sml'])

ReportedEventId: Entity[str] = EntityJsonData(
  type='ReportedEventId',
  path='$.reported_event_id',
  coerce_type=True
)

ReportedPubkey: Entity[str] = EntityJsonData(
  type='ReportedPubkey',
  path='$.reported_pubkey',
  coerce_type=True
)

ReportReason: str = JsonData(
  path='$.report_reason',
  coerce_type=True,
  optional=True
)
