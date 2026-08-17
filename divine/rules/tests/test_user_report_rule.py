"""Guards the profile-only report rule: shape, safety, and wiring.

A NIP-56 report can name an ACCOUNT (a `p` tag) with NO content event (no `e`
tag). Every content report rule guards on `ReportedEvent != ''`, so a
profile-only report matched nothing and reached no moderator. `FirstUserReport`
in `user_report_review.sml` is the one rule that fires on that shape.

This pins the properties that make it correct AND safe:
  * it fires only when there is a reported pubkey and NO event
    (`ReportedPubkeyStr != ''` and `ReportedEvent == ''`), so it never overlaps a
    content report rule;
  * it excludes `underage_user`, which is relay-manager's age-review case system;
  * it declares `flag_for_review` and NOTHING that acts on the account -- no
    `BanNostrEvent`, no `auto_hide` -- because `banpubkey` irreversibly purges and
    humans decide accounts;
  * it is Required from the reports index, or the engine never loads it.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

from pathlib import Path

_REPORTS = Path(__file__).resolve().parent.parent / 'rules' / 'reports'
_RULE = _REPORTS / 'user_report_review.sml'
_INDEX = _REPORTS / 'index.sml'
_MODEL = Path(__file__).resolve().parent.parent / 'models' / 'nostr' / 'kind1984_report.sml'


def _rule_text() -> str:
    return _RULE.read_text()


def test_the_rule_file_exists():
    assert _RULE.exists(), 'user_report_review.sml must exist'


def test_it_fires_only_when_there_is_no_content_event():
    text = _rule_text()
    assert "ReportedEvent == ''" in text, 'must require an EMPTY reported event (profile-only)'
    assert "ReportedPubkeyStr != ''" in text, 'must require a reported account to be present'


def test_it_excludes_age_review():
    assert "ReportReason != 'underage_user'" in _rule_text(), 'age review is relay-manager, not Osprey'


def test_it_only_flags_and_never_acts_on_the_account():
    # Check the DECLARED verdict, not raw file text: the header comment names the
    # sibling file `auto_hide.sml`, so a bare substring test would false-positive.
    text = _rule_text()
    assert "DeclareVerdict(verdict='flag_for_review')" in text
    assert 'BanNostrEvent' not in text, 'a profile-only report must never issue an account/event ban'
    assert "verdict='auto_hide'" not in text, 'there is no reversible account action; humans decide accounts'
    assert "verdict='ban'" not in text, 'humans decide accounts; osprey has no account-ban path'


def test_it_dedups_on_the_reported_pubkey_not_an_event():
    text = _rule_text()
    assert "LabelAdd(entity=ReportedPubkey, label='user_reported')" in text
    assert "not HasLabel(entity=ReportedPubkey, label='user_reported')" in text


def test_the_model_declares_the_plain_string_reported_pubkey():
    assert 'ReportedPubkeyStr' in _MODEL.read_text(), 'the != guard needs a plain-string feature'


def test_the_rule_is_required_from_the_reports_index():
    assert 'user_report_review.sml' in _INDEX.read_text(), 'the engine loads only Required rules'
