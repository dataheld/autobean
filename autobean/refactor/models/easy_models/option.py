from typing import Type, TypeVar
from autobean.refactor.models.raw_models import option
from autobean.refactor.models.raw_models.option import OptionLabel
from . import internal
from .escaped_string import EscapedString

internal.token_model(OptionLabel)

_Self = TypeVar('_Self', bound='Option')


@internal.tree_model
class Option(option.Option):
    key = internal.required_string_property(option.Option.raw_key)
    value = internal.required_string_property(option.Option.raw_value)

    @classmethod
    def from_value(cls: Type[_Self], key: str, value: str) -> _Self:
        return cls.from_children(
            EscapedString.from_value(key),
            EscapedString.from_value(value))
