from typing import Optional

from media_hash import normalize_media_hash
from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase


class NormalizeMediaHashArguments(ArgumentsBase):
    sha256: Optional[str] = None
    """A media hash from a label or a video event, which may be absent or malformed."""


class NormalizeMediaHash(UDFBase[NormalizeMediaHashArguments, str]):
    """The canonical spelling of a media hash, for use as an entity id.

    Exists so that a hash-keyed entity means the same thing whichever path
    produced it. A label carries the hash in its `x` tag and a video event
    carries it in its own `x` tag; both are third-party input and neither
    guarantees case. Building the entity id from the raw value would make
    `DD44...` and `dd44...` two different entities, so a `human_reviewed` label
    written from one path would be invisible to the rule reading the other, and
    the guard would silently do nothing.

    That is the same divergence the sink normalises to avoid on the enforcement
    call. An entity id is a record rather than a call, but the failure is worse:
    a call that misses is retried and logged, while a label written to the wrong
    entity is simply never found.

    Normalisation only. Validity is IsValidMediaHash's job, and a malformed value
    returns lowercased rather than empty so an unusable hash stays
    distinguishable from an absent one.
    """

    def execute(self, execution_context: ExecutionContext, arguments: NormalizeMediaHashArguments) -> str:
        return normalize_media_hash(arguments.sha256)
