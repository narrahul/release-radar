"""A file designed to fool a keyword scan.

It mentions BaseSettings in prose and defines a local class with the same
name, but it never imports pydantic.
"""

TEXT = "BaseSettings was removed in pydantic 2.0"


class BaseSettings:
    """Our own class. Unrelated to the dependency."""

    pass
