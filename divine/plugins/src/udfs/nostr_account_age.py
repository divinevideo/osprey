import time

from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase


class NostrAccountAgeArguments(ArgumentsBase):
    created_at: int
    """The created_at timestamp from a kind 0 metadata event (Unix seconds)."""


class NostrAccountAge(UDFBase[NostrAccountAgeArguments, int]):
    """Returns the account age in seconds based on the kind 0 created_at field."""

    def execute(self, execution_context: ExecutionContext, arguments: NostrAccountAgeArguments) -> int:
        now = int(time.time())
        return max(0, now - arguments.created_at)
