from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / 'rules' / 'content' / 'ai_detector_nsfw.sml'
MODEL = ROOT / 'models' / 'ai_detector_nsfw.sml'
SCHEMA = ROOT.parents[0] / 'clickhouse-schema' / '001_osprey_events.sql'


def test_detector_rule_is_review_only():
    text = RULE.read_text()

    assert "ActionName == 'ai_detector_nsfw'" in text
    assert "DeclareVerdict(verdict='flag_for_review')" in text
    for enforcement_effect in (
        'BanNostrEvent',
        'LabelAdd',
        "verdict='ban'",
        "verdict='restrict'",
        "verdict='auto_hide'",
    ):
        assert enforcement_effect not in text


def test_clickhouse_schema_covers_every_detector_feature_and_rule():
    model = MODEL.read_text()
    schema = SCHEMA.read_text()
    feature_names = [line.split(':', 1)[0] for line in model.splitlines() if ': ' in line and '= JsonData(' in line]

    assert feature_names
    for feature in feature_names + ['DetectorNsfwEvidence']:
        assert f'`{feature}`' in schema
