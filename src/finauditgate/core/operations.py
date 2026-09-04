"""The allowlisted calculations, in one place, for both profile evaluators.

The two evaluators reach their arithmetic by different routes and keep
deliberately different ledgers, so this module deliberately does **not** know
about policies, nodes, roles or artifacts.  It takes two numbers that a caller
has already resolved and returns the one result the named operation defines.
That is the whole of the shared surface: the callers keep their own ordering,
their own reason codes and their own records, and the arithmetic cannot drift
between them.
"""

from __future__ import annotations

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from decimal import DivisionByZero, InvalidOperation, Overflow


# `growth_rate_percent` answers in PERCENT.  `absolute_change` answers in
# whatever the operands are denominated in, which the caller knows and this
# module does not, so it is named rather than computed here.
OPERAND_UNIT = "OPERAND_UNIT"
_OUTPUT_UNITS = {
    "growth_rate_percent": "PERCENT",
    "absolute_change": OPERAND_UNIT,
}
ALLOWLISTED_OPERATIONS = tuple(_OUTPUT_UNITS)

# What a task asks for, per operation.  A task and the profile that decides it
# must agree, or the artifacts would describe an answer nobody asked for.
ANSWER_CONTRACTS = {
    "growth_rate_percent": "PERCENTAGE_CHANGE",
    "absolute_change": "ABSOLUTE_CHANGE",
}
ANSWER_CONTRACT_VALUES = tuple(dict.fromkeys(ANSWER_CONTRACTS.values()))


class OperationDomainError(ValueError):
    """The operands are outside the operation's domain (a zero divisor)."""


def output_unit_for(operation: str, operand_unit: str) -> str:
    """The unit the answer carries, given what the operands are denominated in."""

    declared = _OUTPUT_UNITS[operation]
    return operand_unit if declared is OPERAND_UNIT else declared


def evaluate(
    operation: str,
    *,
    current: Decimal,
    comparison: Decimal,
    quantize: str,
    precision: int,
    emin: int,
    emax: int,
    capitals: int,
    clamp: int,
) -> Decimal:
    """Compute one allowlisted operation under an explicit Decimal context.

    The context is passed in rather than read from a policy, because the two
    callers store the same numbers in differently shaped policies and neither
    policy's bytes may change.
    """

    if operation not in _OUTPUT_UNITS:
        raise KeyError(operation)
    if operation == "growth_rate_percent" and comparison == 0:
        raise OperationDomainError("zero comparison value")
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=emin,
        Emax=emax,
        capitals=capitals,
        clamp=clamp,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context):
        if operation == "growth_rate_percent":
            raw = (current - comparison) / comparison * Decimal("100")
        else:
            raw = current - comparison
        return raw.quantize(Decimal(quantize), rounding=ROUND_HALF_EVEN)
