class DomainError(Exception):
    """A business rule was violated. Message is safe to show to users."""


class InsufficientStock(DomainError):
    pass


class IdempotencyConflict(DomainError):
    pass


class ImmutableRecordError(DomainError):
    pass
