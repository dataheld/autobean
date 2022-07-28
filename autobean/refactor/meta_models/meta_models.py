# pylance: disable
# type: ignore

from typing import Optional, Union
from .base import MetaModel, Floating, field


# Auxiliary


class Amount(MetaModel):
    number: 'number_expr'
    currency: 'CURRENCY'


class Tolerance(MetaModel):
    _tilde: 'TILDE' = field(define_as='Tilde')
    number: 'number_expr'


# Directives


class Include(MetaModel):
    _label: 'INCLUDE' = field(define_as='IncludeLabel')
    filename: 'ESCAPED_STRING'


class Option(MetaModel):
    _label: 'OPTION' = field(define_as='OptionLabel')
    key: 'ESCAPED_STRING'
    value: 'ESCAPED_STRING'


class Plugin(MetaModel):
    _label: 'PLUGIN' = field(define_as='PluginLabel')
    name: 'ESCAPED_STRING'
    config: Optional['ESCAPED_STRING'] = field(floating=Floating.LEFT)


class Popmeta(MetaModel):
    _label: 'POPMETA' = field(define_as='PopmetaLabel')
    key: 'META_KEY'


class Poptag(MetaModel):
    _label: 'POPTAG' = field(define_as='PoptagLabel')
    tag: 'TAG'


class Pushmeta(MetaModel):
    _label: 'PUSHMETA' = field(define_as='PushmetaLabel')
    key: 'META_KEY'
    value: Optional[Union[
        'ESCAPED_STRING',
        'ACCOUNT',
        'DATE',
        'CURRENCY',
        'TAG',
        'BOOL',
        'NULL',
        'number_expr',
        'amount',
    ]] = field(floating=Floating.LEFT, type_alias='MetaValue')


class Pushtag(MetaModel):
    _label: 'PUSHTAG' = field(define_as='PushtagLabel')
    tag: 'TAG'


# Directives


class Balance(MetaModel):
    date: 'DATE'
    _label: 'BALANCE' = field(define_as='BalanceLabel')
    account: 'ACCOUNT'
    number: 'number_expr'
    tolerance: Optional['tolerance'] = field(floating=Floating.LEFT)
    currency: 'CURRENCY'


class Close(MetaModel):
    date: 'DATE'
    _label: 'CLOSE' = field(define_as='CloseLabel')
    account: 'ACCOUNT'


class Commodity(MetaModel):
    date: 'DATE'
    _label: 'COMMODITY' = field(define_as='CommodityLabel')
    currency: 'CURRENCY'


class Event(MetaModel):
    date: 'DATE'
    _label: 'EVENT' = field(define_as='EventLabel')
    type: 'ESCAPED_STRING'
    description: 'ESCAPED_STRING'


class Pad(MetaModel):
    date: 'DATE'
    _label: 'PAD' = field(define_as='PadLabel')
    account: 'ACCOUNT'
    source_account: 'ACCOUNT'


class Query(MetaModel):
    date: 'DATE'
    _label: 'QUERY' = field(define_as='QueryLabel')
    name: 'ESCAPED_STRING'
    query_string: 'ESCAPED_STRING'