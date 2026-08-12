from osprey.engine.executor.execution_context import ExecutionContext
from osprey.engine.udf.arguments import ArgumentsBase
from osprey.engine.udf.base import UDFBase
from trusted_moderation import is_trusted_moderation_signer


class IsTrustedModerationSignerArguments(ArgumentsBase):
    pubkey: str
    """The pubkey that signed the kind 1985 label event."""


class IsTrustedModerationSigner(UDFBase[IsTrustedModerationSignerArguments, bool]):
    """Whether a kind 1985 label was signed by a moderation identity this environment trusts.

    The trusted set comes from `DIVINE_TRUSTED_MODERATION_PUBKEYS` and defaults to
    the production identity. See `trusted_moderation` for the semantics, including
    why an override replaces rather than extends the default.
    """

    def execute(self, execution_context: ExecutionContext, arguments: IsTrustedModerationSignerArguments) -> bool:
        return is_trusted_moderation_signer(arguments.pubkey)
