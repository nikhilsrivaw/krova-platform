"""
Column types.

Enum columns are stored as plain strings rather than Postgres ENUM types:
adding a value to a native ENUM needs a migration and an exclusive lock, and
these lists grow (a new channel, a new commitment kind). Strings keep that
cheap.

The cost of plain String columns is that SQLAlchemy writes the enum's value
but hands back a bare str on read - so `message.direction` is typed as an
enum, prints like an enum in comparisons, and then raises on `.value`. This
decorator closes that gap: values go in as strings, come back as real enum
members, and the type annotation stops lying.
"""

import enum
from typing import Any, TypeVar

from sqlalchemy import String, TypeDecorator

E = TypeVar("E", bound=enum.Enum)


class EnumType(TypeDecorator):
    """A string column that round-trips a Python Enum."""

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[enum.Enum], length: int = 32, **kwargs: Any):
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Accept the raw string too - callers and fixtures often pass one, and
        # rejecting it would be pedantry rather than safety.
        if isinstance(value, str):
            return self.enum_class(value).value
        raise ValueError(f"Cannot store {value!r} as {self.enum_class.__name__}")

    def process_result_value(self, value: Any, dialect: Any) -> enum.Enum | None:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by a newer version of the code than this one is
            # running. Returning the raw string keeps the row readable instead
            # of making every query on the table fail.
            return value
