import collections
import dataclasses
import decimal
import itertools
from typing import Any, Iterator, Optional, TypeVar
from beancount.core import account as account_lib, amount as amount_lib, inventory as inventory_lib, convert, interpolate, realization
from beancount.core.data import Balance, Close, Custom, Directive, Open, Posting, Transaction
from beancount.core.amount import Amount
from beancount.core.position import Cost, CostSpec
from beancount.ops import balance as balance_lib
from autobean.utils import error_lib
from . import policy_lib, viewpoint_lib

_CONVERSION_HACK_LABEL = 'autobean.share conversion hack'
# TODO: consider determining the tolerance in a better way
_PROPORTIONATE_TOLERANCE = decimal.Decimal(1e-6)
_O = TypeVar('_O', bound=policy_lib.Ownership)
_PostingPolicy = tuple[Posting, policy_lib.Policy[_O]]


def _amount_distrib(amount: Amount, weight: decimal.Decimal, total_weight: decimal.Decimal) -> Amount:
    return amount_lib.div(amount_lib.mul(amount, weight), total_weight)


def _costspec_distrib(costspec: CostSpec, weight: decimal.Decimal, total_weight: decimal.Decimal) -> CostSpec:
    number_total = costspec.number_total
    if number_total is not None:
        amount = Amount(number_total, costspec.currency)
        number_total = _amount_distrib(amount, weight, total_weight)
    return costspec._replace(
        number_total=number_total,
    )


def _posting_distrib(posting: Posting, weight: decimal.Decimal, total_weight: decimal.Decimal) -> Posting:
    units = _amount_distrib(posting.units, weight, total_weight)
    cost = posting.cost
    if isinstance(cost, CostSpec):
        cost = _costspec_distrib(cost, weight, total_weight)
    return posting._replace(
        units=units,
        cost=cost,
    )


def _split_posting_weighted(
        posting: Posting,
        ownership: policy_lib.WeightedOwnership,
) -> dict[str, Posting]:
    return {
        party: _posting_distrib(posting, weight, ownership.total_weight)
        for party, weight in ownership.weights.items()
    }


def _generate_posting_pair(posting: Posting, policy: policy_lib.Policy) -> tuple[Posting, Posting]:
    units = convert.get_weight(posting)
    if policy.conversion or (not posting.price and not posting.cost):
        return posting, posting._replace(units=units, price=None, cost=None)
    if posting.cost:
        raise error_lib.PluginException('share_conversion: FALSE is not supported for postings with cost')
    # We hold the price information in cost as beancount inventory doesn't support price
    inverse_price = Cost(
        number = 1 / posting.price.number,
        currency=posting.units.currency,
        label=_CONVERSION_HACK_LABEL)
    return posting._replace(price=None), posting._replace(units=units, price=None, cost=inverse_price)


@dataclasses.dataclass(frozen=True)
class _GroupedPostings:
    weighted: list[_PostingPolicy[policy_lib.WeightedOwnership]]
    prorated: list[_PostingPolicy[policy_lib.ProratedOwnership]]

    @classmethod
    def from_transaction(
            cls,
            transaction: Transaction,
            policy_db: policy_lib.PolicyDatabase,
    ) -> '_GroupedPostings':
        transaction_policy_def = policy_lib.try_parse_policy_definition(transaction.meta)
        policy_lib.strip_share_meta(transaction.meta)
        weighted_postings_policies = list[_PostingPolicy[policy_lib.WeightedOwnership]]()
        prorated_postings_policies = list[_PostingPolicy[policy_lib.ProratedOwnership]]()
        for posting in transaction.postings:
            policy = policy_db.get_posting_policy(posting, transaction_policy_def)
            if policy is None:
                raise error_lib.PluginException('No applicable share policy')
            if isinstance(policy.ownership, policy_lib.WeightedOwnership):
                weighted_postings_policies.append((posting, policy))
            elif isinstance(policy.ownership, policy_lib.ProratedOwnership):
                prorated_postings_policies.append((posting, policy))
            else:
                assert False
        return cls(weighted_postings_policies, prorated_postings_policies)


def _complement_posting_from_position(
        position: inventory_lib.Position,
        account: str,
) -> Posting:
    if position.cost and position.cost.label == _CONVERSION_HACK_LABEL:
        cost = None
        price = Amount(position.cost.number, position.cost.currency)
    else:
        cost = position.cost
        price = None
    return Posting(
        account=account,
        units=position.units,
        price=price,
        cost=cost,
        flag=None,
        meta=(),
    )


class _ProratedOwnershipBuilder:
    def __init__(self) -> None:
        self._currency: Optional[str] = None
        self._weights = collections.defaultdict[str, decimal.Decimal](decimal.Decimal)
        self._total_weights = decimal.Decimal(0)

    def check_currency(self, currency: str) -> None:
        if self._currency is None:
            self._currency = currency
        elif self._currency != currency:
            raise error_lib.PluginException(
                f'Currency mismatch in prorated weights calculation: '
                f'{currency} != {self._currency}')

    def add_postings(self, postings: dict[str, Posting]) -> None:
        for party, posting in postings.items():
            self._weights[party] += posting.units.number
            self._total_weights += posting.units.number

    def build(self) -> policy_lib.WeightedOwnership:
        if not self._weights:
            raise error_lib.PluginException(
                f'Cannot determine prorated ownership weights with no participating postings')
        if not self._total_weights:
            raise error_lib.PluginException(
                f'Cannot determine prorated ownership weights with zero total weights')
        return policy_lib.WeightedOwnership(self._weights)


class _TransactionProcessor:
    def __init__(self, *, receivable_account: str) -> None:
        self._receivable_account = receivable_account
        self._postings = list[Posting]()
        self._postings_by_party = collections.defaultdict[str, list[Posting]](list)
        self._inventory_by_party = collections.defaultdict[str, inventory_lib.Inventory](inventory_lib.Inventory)
        # complement receivable postings generated from explicit postings on receivables
        self._complement_receivables = collections.defaultdict[str, list[Posting]](list)

    def _add_weighted_posting(
            self,
            posting: Posting,
            policy: policy_lib.Policy,
            ownership: policy_lib.WeightedOwnership,
    ) -> dict[str, Posting]:
        policy_lib.strip_share_meta(posting.meta)
        posting, complement = _generate_posting_pair(posting, policy)
        party_postings = _split_posting_weighted(posting, ownership)
        for party, posting in party_postings.items():
            self._postings_by_party[party].append(posting)
        complement_party_postings = _split_posting_weighted(complement, ownership)
        for party, posting in complement_party_postings.items():
            self._inventory_by_party[party].add_amount(posting.units, posting.cost)
        parent, _, receivable_party = posting.account.rpartition(':')
        if parent == self._receivable_account:
            for party, posting in party_postings.items():
                self._complement_receivables[receivable_party].append(posting._replace(
                    account=f'{self._receivable_account}:{party}',
                    units=-posting.units,
                ))
        return party_postings

    def process_transaction(
            self,
            transaction: Transaction,
            policy_db: policy_lib.PolicyDatabase,
            options: dict[str, Any],
    ) -> None:
        self._postings = transaction.postings
        self._tolerance = interpolate.infer_tolerances(transaction.postings, options)
        grouped_postings = _GroupedPostings.from_transaction(transaction, policy_db)
        prorated_ownership_builder = _ProratedOwnershipBuilder()
        for posting, weighted_policy in grouped_postings.weighted:
            party_postings = self._add_weighted_posting(posting, weighted_policy, weighted_policy.ownership)
            if grouped_postings.prorated:
                prorated_ownership_builder.check_currency(posting.units.currency)
                if weighted_policy.prorated_included:
                    prorated_ownership_builder.add_postings(party_postings)
        if grouped_postings.prorated:
            prorated_ownership = prorated_ownership_builder.build()
            for posting, prorated_policy in grouped_postings.prorated:
                self._add_weighted_posting(posting, prorated_policy, prorated_ownership)

    def realize(self, root: realization.RealAccount, accounts: set[str]) -> None:
        for posting in self._postings:
            if posting.account in accounts:
                realization.get_or_create(
                    root, posting.account,
                ).balance.add_position(posting)

    def realize_by_party(self, roots: dict[str, realization.RealAccount], accounts: set[str]) -> None:
        for party, postings in self._postings_by_party.items():
            for posting in postings:
                if posting.account in accounts:
                    realization.get_or_create(
                        roots[party], posting.account,
                    ).balance.add_position(posting)

    def get_postings(
            self,
            *,
            viewpoint: str,
            used_subaccounts: dict[str, set[str]],
    ) -> list[Posting]:
        if viewpoint == viewpoint_lib.NOBODY:
            return [
                *self._postings,
                *itertools.chain.from_iterable(self._complement_receivables.values()),
                *self._get_complement_postings(),
            ]
        if viewpoint == viewpoint_lib.EVERYONE:
            return [
                *self._get_split_postings(used_subaccounts=used_subaccounts),
                *itertools.chain.from_iterable(self._complement_receivables.values()),
                *self._get_complement_postings(),
            ]
        ret = [
            *self._postings_by_party[viewpoint],
            *self._complement_receivables[viewpoint],
        ]
        if self._postings_by_party[viewpoint]:
            ret += self._get_complement_postings(excluded_party=viewpoint)
        return ret

    def _get_split_postings(
            self,
            *,
            used_subaccounts: dict[str, set[str]],
    ) -> Iterator[Posting]:
        for party, postings in self._postings_by_party.items():
            for posting in postings:
                account = posting.account
                parent, _, _ = account.rpartition(':')
                if parent != self._receivable_account:
                    account = f'{posting.account}:[{party}]'
                    used_subaccounts[posting.account].add(account)
                    yield posting._replace(account=account)
                else:
                    yield posting

    def _get_complement_postings(
            self,
            *,
            excluded_party: Optional[str] = None) -> list[Posting]:
        return [
            _complement_posting_from_position(position, f'{self._receivable_account}:{party}')
            for party, inventory in self._inventory_by_party.items()
            if party != excluded_party and not inventory.is_small(self._tolerance)
            for position in inventory
        ]


class AccountSplitter:
    def __init__(
            self,
            policy_db: policy_lib.PolicyDatabase,
            options: dict[str, Any],
            viewpoint: str,
            asserted_accounts: set[str],
    ) -> None:
        self._policy_db = policy_db
        self._options = options
        self._viewpoint = viewpoint
        self._asserted_accounts = asserted_accounts
        self._overall_real_root = realization.RealAccount('')
        self._party_real_roots = collections.defaultdict[str, realization.RealAccount](
            lambda: realization.RealAccount(''))
        self._used_subaccounts = collections.defaultdict[str, set[str]](set)

    def process_transaction(self, transaction: Transaction, receivable_account: str) -> Optional[Transaction]:
        processor = _TransactionProcessor(receivable_account=receivable_account)
        processor.process_transaction(transaction, self._policy_db, self._options)
        asserted_accounts = {
            posting.account
            for posting in transaction.postings
            if any(account in self._asserted_accounts for account in account_lib.parents(posting.account))
        }
        processor.realize(self._overall_real_root, asserted_accounts)
        processor.realize_by_party(self._party_real_roots, asserted_accounts)
        postings = processor.get_postings(
            viewpoint=self._viewpoint,
            used_subaccounts=self._used_subaccounts)
        if transaction.postings and not postings:
            # irrelevant to our viewpoint
            return None
        policy_lib.strip_share_meta(transaction.meta)
        return transaction._replace(postings=postings)

    def process_balance(self, balance: Balance, error_logger: error_lib.ErrorLogger) -> list[Balance]:
        if self._viewpoint == viewpoint_lib.NOBODY:
            policy_lib.strip_share_meta(balance.meta)
            return [balance]
        tolerance = balance_lib.get_balance_tolerance(balance, self._options)
        real_account = realization.get(self._overall_real_root, balance.account)
        _check_balance(real_account, balance, balance.amount, tolerance, error_logger)
        policy = self._policy_db.get_balance_policy(balance)
        policy_lib.strip_share_meta(balance.meta)
        if not policy:
            return []
        if self._viewpoint == viewpoint_lib.EVERYONE:
            return [
                balance._replace(
                    account=f'{balance.account}:[{party}]',
                    amount=_amount_distrib(balance.amount, weight, policy.ownership.total_weight),
                )
                for party, weight in policy.ownership.weights.items()
            ]
        for party, weight in policy.ownership.weights.items():
            if party == self._viewpoint:
                continue  # will be checked by the returned balance directive
            real_account = realization.get(self._party_real_roots[party], balance.account)
            balance_amount = _amount_distrib(balance.amount, weight, policy.ownership.total_weight)
            _check_balance(real_account, balance, balance_amount, tolerance, error_logger)
        if self._viewpoint not in policy.ownership.weights:
            return []
        return [
            balance._replace(amount=_amount_distrib(
                balance.amount,
                policy.ownership.weights[self._viewpoint],
                policy.ownership.total_weight)),
        ]

    def process_proportionate(self, entry: Custom, account: str) -> Optional[Custom]:
        policy = self._policy_db.get_proportionate_policy(entry, account)
        if not policy:
            raise error_lib.PluginException(
                f'No applicable share policy found for autobean.share.proportionate on {account}')
        if len(policy.ownership.weights) > 1:
            # single party owner is by construction proportionate
            _check_proportionate(account, policy, self._overall_real_root, self._party_real_roots)
        if viewpoint_lib.is_overall(self._viewpoint):
            policy_lib.strip_share_meta(entry.meta)
            return entry
        return None

    def process_open_close(self, entries: list[Directive]) -> list[Directive]:
        if self._viewpoint != viewpoint_lib.EVERYONE:
            return entries
        results = []
        for entry in entries:
            if isinstance(entry, Open | Close):
                for subaccount in sorted(self._used_subaccounts.get(entry.account, (entry.account,))):
                    results.append(entry._replace(account=subaccount))
            else:
                results.append(entry)
        return results


def _check_balance(
        real_account: realization.RealAccount,
        balance: Balance,
        expected_amount: Amount,
        tolerance: decimal.Decimal,
        error_logger: error_lib.ErrorLogger,
) -> None:
    subtree_balance = realization.compute_balance(real_account, leaf_only=False)
    actual_amount = subtree_balance.get_currency_units(expected_amount.currency)
    diff_amount = amount_lib.sub(actual_amount, expected_amount)
    if abs(diff_amount.number) > tolerance:
        diff_direction = 'too much' if diff_amount.number > 0 else 'too little'
        error_logger.log_error(balance_lib.BalanceError(
            balance.meta,
            f'Balance failed for {balance.account!r}: '
            f'expected {expected_amount} != accumulated {actual_amount} '
            f'({abs(diff_amount.number)} {diff_direction})',
            balance,
        ))


def _check_proportionate(
        account: str,
        policy: policy_lib.Policy[policy_lib.WeightedOwnership],
        overall_real_root: realization.RealAccount,
        party_real_roots: dict[str, realization.RealAccount],
) -> None:
    real_account = realization.get(overall_real_root, account)
    if real_account is None:
        return  # empty account is by definition proportionate

    subtree_balance = realization.compute_balance(real_account, leaf_only=False)
    party_subtree_balances = {}
    for party, party_real_root in party_real_roots.items():
        if (party_real_account := realization.get(party_real_root, account)) is not None:
            party_subtree_balance = realization.compute_balance(
                party_real_account, leaf_only=False)
        else:
            party_subtree_balance = inventory_lib.Inventory()
        if party in policy.ownership.weights:
            party_subtree_balances[party] = party_subtree_balance
        elif not party_subtree_balance.is_small(_PROPORTIONATE_TOLERANCE):
            raise error_lib.PluginException(f'Disproportionate balance on account {account}')
        party_subtree_balances[party] = party_subtree_balance
    for key, position in subtree_balance.items():
        for party, weight in policy.ownership.weights.items():
            expected_num = position.units.number * weight / policy.ownership.total_weight
            actual_num = 0
            if (party_subtree_balance := party_subtree_balances.get(party)) is not None:
                if party_position := party_subtree_balance.get(key):
                    actual_num = party_position.units.number
            diff_num = actual_num - expected_num
            if abs(diff_num) > _PROPORTIONATE_TOLERANCE:
                raise error_lib.PluginException(f'Disproportionate balance on account {account}')