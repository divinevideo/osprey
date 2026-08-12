from dataclasses import dataclass
from typing import List, Self, cast

from media_hash import normalize_media_hash
from osprey.engine.executor.custom_extracted_features import CustomExtractedFeature
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.language_types.effects import EffectBase, EffectToCustomExtractedFeatureBase
from osprey.engine.stdlib.udfs.categories import UdfCategories
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase
from osprey.engine.utils.types import add_slots


class AgeRestrictNostrEventArguments(ArgumentsBase):
    event_id: str
    sha256: str
    reason: str


@dataclass
class AgeRestrictEffect(EffectToCustomExtractedFeatureBase[List[str]]):
    """Effect requesting age-restriction of media via relay-manager's moderate-media endpoint."""

    event_id: str
    sha256: str
    reason: str

    def to_str(self) -> str:
        return f'{self.event_id}|{self.sha256}|{self.reason}'

    @classmethod
    def build_custom_extracted_feature_from_list(cls, values: List[Self]) -> CustomExtractedFeature[List[str]]:
        return AgeRestrictEffectsExtractedFeature(effects=cast(List[AgeRestrictEffect], values))


@add_slots
@dataclass
class AgeRestrictEffectsExtractedFeature(CustomExtractedFeature[List[str]]):
    effects: List[AgeRestrictEffect]

    @classmethod
    def feature_name(cls) -> str:
        return 'age_restrict_nostr_event'

    def get_serializable_feature(self) -> List[str] | None:
        return [effect.to_str() for effect in self.effects]


def synthesize_effect(arguments: AgeRestrictNostrEventArguments) -> AgeRestrictEffect:
    # Normalise here, at the point the effect is created, rather than only where
    # it is sent. The effect is recorded as well as acted on: `to_str` feeds the
    # __age_restrict_nostr_event column, whose purpose is to make the enforcement
    # that actually fired queryable, and to reconcile Osprey's record against
    # moderation-service's. Normalising only in the sink left those two disagreeing
    # for an uppercase label hash: the payload carried dd44..., the column said
    # DD44..., and the reconciliation the column exists for silently found nothing.
    #
    # Doing it once here covers both the call and the record. See media_hash.py for
    # why the canonical spelling matters downstream.
    return AgeRestrictEffect(
        event_id=arguments.event_id,
        sha256=normalize_media_hash(arguments.sha256),
        reason=arguments.reason,
    )


class AgeRestrictNostrEvent(UDFBase[AgeRestrictNostrEventArguments, EffectBase]):
    category = UdfCategories.ENGINE

    def execute(self, execution_context: ExecutionContext, arguments: AgeRestrictNostrEventArguments) -> EffectBase:
        return synthesize_effect(arguments)
