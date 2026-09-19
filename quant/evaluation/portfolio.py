"""Evaluate supplied daily portfolio P&L; no trading simulator is implied."""

from dataclasses import dataclass
from datetime import date
import math
import statistics

from quant.contracts import finite, require


@dataclass(frozen=True)
class PortfolioDay:
    session: date
    gross_return: float
    cost_return: float
    risk_free_return: float
    turnover: float

    def __post_init__(self):
        require(type(self.session) is date, "PORTFOLIO_DATE_REQUIRED")
        require(all(finite(v) for v in (self.gross_return, self.cost_return,
                self.risk_free_return, self.turnover)), "PORTFOLIO_NONFINITE")
        require(self.cost_return >= 0 and self.turnover >= 0
                and self.gross_return - self.cost_return > -1, "PORTFOLIO_RETURN_INVALID")


def evaluate_portfolio(days, *, return_horizon_sessions, method):
    require(type(return_horizon_sessions) is int and return_horizon_sessions == 1,
            "DAILY_PNL_REQUIRED_NOT_FORWARD_LABEL_RETURNS")
    require(len(days) >= 2 and bool(method), "PORTFOLIO_METHOD_AND_DAILY_SERIES_REQUIRED")
    require(tuple(sorted({d.session for d in days})) == tuple(d.session for d in days),
            "PORTFOLIO_DATES_NOT_UNIQUE_SORTED")
    net = [d.gross_return - d.cost_return for d in days]
    excess = [r - d.risk_free_return for r, d in zip(net, days)]
    std = statistics.stdev(excess)
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in net:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {"method": method, "observations": len(days), "net_cumulative_return": equity - 1,
            "annualized_sharpe_252": math.sqrt(252) * statistics.fmean(excess) / std if std else None,
            "max_drawdown": drawdown, "mean_daily_turnover": statistics.fmean(d.turnover for d in days),
            "limitations": ["252-session square-root annualization is a convention, not an autocorrelation correction.",
                            "Execution feasibility and cohort capital accounting belong to the supplied P&L producer."]}
