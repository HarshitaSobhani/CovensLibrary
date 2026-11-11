class RuleViolation(Exception):
    """A business rule was violated. `code` is stable for tests; `message` is shown to the owner."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
