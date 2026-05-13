from dataclasses import dataclass
from typing import List, Self, cast

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
    return AgeRestrictEffect(
        event_id=arguments.event_id,
        sha256=arguments.sha256,
        reason=arguments.reason,
    )


class AgeRestrictNostrEvent(UDFBase[AgeRestrictNostrEventArguments, EffectBase]):
    category = UdfCategories.ENGINE

    def execute(self, execution_context: ExecutionContext, arguments: AgeRestrictNostrEventArguments) -> EffectBase:
        return synthesize_effect(arguments)
