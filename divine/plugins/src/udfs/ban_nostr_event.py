from dataclasses import dataclass
from typing import List, Self, cast

from osprey.engine.executor.custom_extracted_features import CustomExtractedFeature
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.language_types.effects import EffectBase, EffectToCustomExtractedFeatureBase
from osprey.engine.stdlib.udfs.categories import UdfCategories
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase
from osprey.engine.utils.types import add_slots


class BanNostrEventArguments(ArgumentsBase):
    event_id: str
    pubkey: str
    reason: str


@dataclass
class BanEventEffect(EffectToCustomExtractedFeatureBase[List[str]]):
    """Effect representing a request to ban a Nostr event via relay manager."""

    event_id: str
    """The Nostr event ID to ban."""

    pubkey: str
    """The pubkey of the event author."""

    reason: str
    """Reason for banning the event."""

    def to_str(self) -> str:
        return f'{self.event_id}|{self.pubkey}|{self.reason}'

    @classmethod
    def build_custom_extracted_feature_from_list(cls, values: List[Self]) -> CustomExtractedFeature[List[str]]:
        return BanEventEffectsExtractedFeature(effects=cast(List[BanEventEffect], values))


@add_slots
@dataclass
class BanEventEffectsExtractedFeature(CustomExtractedFeature[List[str]]):
    effects: List[BanEventEffect]

    @classmethod
    def feature_name(cls) -> str:
        return 'ban_nostr_event'

    def get_serializable_feature(self) -> List[str] | None:
        return [effect.to_str() for effect in self.effects]


def synthesize_effect(arguments: BanNostrEventArguments) -> BanEventEffect:
    return BanEventEffect(
        event_id=arguments.event_id,
        pubkey=arguments.pubkey,
        reason=arguments.reason,
    )


class BanNostrEvent(UDFBase[BanNostrEventArguments, EffectBase]):
    category = UdfCategories.ENGINE

    def execute(self, execution_context: ExecutionContext, arguments: BanNostrEventArguments) -> EffectBase:
        return synthesize_effect(arguments)
