# Behavioral rules for Nostr events

Import(rules=['models/base.sml'])

Require(rule='rules/behavioral/new_account_spam.sml')
Require(rule='rules/behavioral/repeat_offender.sml')
Require(rule='rules/behavioral/rapid_posting.sml')
