"""Average ties, ascending rank, and one explicit percentile convention."""

from quant.contracts import finite, require


def average_ranks(values):
    require(bool(values) and all(finite(v) for v in values), "RANK_INPUT_INVALID")
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in ordered[start:end]:
            ranks[index] = rank
        start = end
    return tuple(ranks)


def percentiles(values):
    require(len(values) >= 2, "PERCENTILE_REQUIRES_CROSS_SECTION")
    return tuple((r - 1) / (len(values) - 1) for r in average_ranks(values))
