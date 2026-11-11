class FocusLockError(Exception):
    """Base class for errors that are safe to show to the user."""


class ConfigError(FocusLockError):
    pass


class InvalidDomainError(FocusLockError, ValueError):
    pass


class HostsCorruptError(FocusLockError):
    pass


class InvalidTransitionError(FocusLockError):
    pass


class SessionConflictError(FocusLockError):
    pass


class RewardError(FocusLockError):
    pass


class AlreadyRunningError(FocusLockError):
    pass


class PasswordError(FocusLockError):
    pass
