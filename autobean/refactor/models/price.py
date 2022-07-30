import datetime
from typing import Type, TypeVar
from . import internal
from .amount import Amount
from .currency import Currency
from .date import Date
from .generated import price
from .generated.price import PriceLabel

_Self = TypeVar('_Self', bound='Price')


@internal.tree_model
class Price(price.Price):
    date = internal.required_date_property(price.Price.raw_date)
    currency = internal.required_string_property(price.Price.raw_currency)
    amount = price.Price.raw_amount

    @classmethod
    def from_value(
            cls: Type[_Self],
            date: datetime.date,
            currency: str,
            amount: Amount,
    ) -> _Self:
        return cls.from_children(
            Date.from_value(date),
            Currency.from_value(currency),
            amount,
        )