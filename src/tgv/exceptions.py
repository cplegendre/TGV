class TGVError(Exception):
    """Base error for expected TGV failures."""


class ConfigurationError(TGVError):
    """Raised for invalid configuration."""


class RepositoryAccessError(TGVError):
    """Raised when a requested repository path is unsafe or unavailable."""


class ModelProviderError(TGVError):
    """Raised when a model provider cannot complete a request."""


class ProviderUnavailableError(TGVError):
    """Raised when a configured handoff provider is unavailable."""


class CommandExecutionError(TGVError):
    """Raised when an external handoff command fails."""


class ValidationFailure(TGVError):
    """Raised when validation cannot be completed successfully."""
