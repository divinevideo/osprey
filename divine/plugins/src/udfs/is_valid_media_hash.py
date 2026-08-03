from typing import Optional

from media_hash import is_valid_media_hash
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase


class IsValidMediaHashArguments(ArgumentsBase):
    sha256: Optional[str] = None
    """The media hash carried by the label, which may be absent or malformed."""


class IsValidMediaHash(UDFBase[IsValidMediaHashArguments, bool]):
    """Whether a label's media hash is one the enforcement endpoint can act on.

    Gating the enforcement rules on this keeps Osprey's record honest: without
    it, a label carrying a malformed hash still declares a `restrict` verdict and
    writes the `age_restricted` label, while the sink declines to make the call.
    Osprey would then show a restriction that never happened.

    Absent and malformed are treated identically on purpose -- neither is
    actionable -- so the review path needs only one condition to cover both.
    """

    def execute(self, execution_context: ExecutionContext, arguments: IsValidMediaHashArguments) -> bool:
        return is_valid_media_hash(arguments.sha256)
