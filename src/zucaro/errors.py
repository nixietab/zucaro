class PicomcError(Exception):
    """Base class for all zucaro exceptions."""
    pass


class AccountError(PicomcError):
    """Exception raised for account-related errors."""
    pass


class ValidationError(PicomcError):
    """Exception raised for validation errors."""
    pass


class AuthenticationError(PicomcError):
    """Exception raised for authentication errors."""
    pass


class RefreshError(PicomcError):
    """Exception raised for refresh errors."""
    pass


class InstanceNotFoundError(PicomcError):
    """Exception raised when an instance is not found."""
    pass