# Repeat Offender — Graduated Enforcement
# Escalates from warned → suspended → banned.

Import(
  rules=[
    'models/base.sml',
  ]
)

PreviouslyWarned = Rule(
  when_all=[
    HasLabel(entity=Pubkey, label='warned'),
  ],
  description='Account has a prior warning',
)

PreviouslySuspended = Rule(
  when_all=[
    HasLabel(entity=Pubkey, label='suspended'),
  ],
  description='Account is currently suspended',
)

WhenRules(
  rules_any=[PreviouslyWarned],
  then=[
    LabelAdd(entity=Pubkey, label='suspended', expires_after=TimeDelta(days=30)),
    LabelRemove(entity=Pubkey, label='warned'),
    DeclareVerdict(verdict='suspend'),
  ],
)

WhenRules(
  rules_any=[PreviouslySuspended],
  then=[
    LabelAdd(entity=Pubkey, label='banned'),
    BanNostrEvent(event_id=EventId, pubkey=Pubkey, reason='Repeat offender escalation to ban'),
    DeclareVerdict(verdict='ban'),
  ],
)
