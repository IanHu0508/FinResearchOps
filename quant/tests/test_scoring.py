from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from quant.contracts import CHINA, ContractError, Prediction, ResearchSpec, session_of
from quant.data.acquisition import save_response
from quant.data.baostock import decode_table
from quant.data.market_store import MarketStore, UNIVERSE_ID, build_store
from quant.data.records import ResearchData
from quant.features import build_panel
from quant.inference import build_signals, score_day
from quant.inference.signals import inference_input_id
from quant.tests.test_acquisition import frame
from quant.tests.test_market_store import FIELDS

CODES = ("sh.600001", "sh.600002", "sz.000001", "sz.300001")


def price(stock, ordinal):
    return round(10 + stock + 0.4 * math.sin(ordinal / (3 + stock)) + 0.01 * stock * ordinal, 2)


def make_store(directory, session_count=110):
    """Four synthetic stocks with distinct price and activity paths."""
    workspace = Path(directory)
    (workspace / "finaudit-gate").mkdir()
    (workspace / "finaudit-gate" / "pyproject.toml").write_text("# synthetic test workspace\n")
    root = workspace / "private" / "raw"
    root.mkdir(parents=True)
    sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(session_count)]
    plan = {"schema_version": "quant.baostock-acquisition/v1", "start": str(sessions[0]), "end": str(sessions[-1])}
    (root / "plan.json").write_text(json.dumps(plan))
    wire = frame(["0", "ok", "query_trade_dates", "anonymous", "1", "2000",
                  json.dumps({"record": [[str(d), "1"] for d in sessions]}), "", "", "calendar_date,is_trading_day"], "34")
    save_response(root, "calendar-p1", decode_table(wire, method="query_trade_dates", field_index=9), query={}, seconds=0)
    for i, day in enumerate(sessions):
        rows = []
        for stock, code in enumerate(CODES):
            close, previous = price(stock, i), price(stock, max(i - 1, 0))
            volume = 1000 + 100 * ((i * (stock + 1)) % 7)
            rows.append([str(day), code, str(previous), str(max(previous, close) + 0.03), str(min(previous, close) - 0.03),
                         str(close), str(previous), str(volume), str(round(volume * close, 2)), "3", "1", "0"])
        wire = frame(["0", "ok", "query_daily_history_k_AStock", "anonymous",
                      json.dumps({"record": rows}), ",".join(FIELDS)], "99")
        save_response(root, "daily-" + str(day), decode_table(wire, method="query_daily_history_k_AStock",
                                                              row_index=4, field_index=5), query={}, seconds=0)
    destination = workspace / "private" / "canonical.sqlite"
    build_store(root, destination)
    return destination, sessions


def without_later_records(data, day):
    return ResearchData(tuple(s for s in data.sessions if s <= day), tuple(b for b in data.bars if b.session <= day),
                        tuple(u for u in data.universes if session_of(u.as_of) <= day), data.scoring_dates, data.data_kind)


def delete_later_rows(path, day):
    """Physically remove every dated row after `day`; the metadata manifest stays unchanged."""
    connection = sqlite3.connect(path)
    try:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if any(column[1] == "session" for column in connection.execute(f"PRAGMA table_info({name})"))]
        removed = {name: connection.execute(f"DELETE FROM {name} WHERE session>?", (day.isoformat(),)).rowcount
                   for name in tables}
        connection.commit()
        return removed
    finally:
        connection.close()


class StubModel:
    """Deterministic, dependency-free stand-in for a restored fitted model."""

    view, ablation, model_version, artifact = "tabular", "stock+context", "stub-v1", {}

    def __init__(self, training_cutoff=datetime(2020, 1, 1, tzinfo=CHINA)):
        self.training_cutoff = training_cutoff

    def predict(self, rows):
        return tuple(Prediction(row.key, min(1.0, max(0.0, 0.5 + row.scalars[2] + row.relative_features[1])))
                     for row in rows)


class ScoringTests(unittest.TestCase):
    def test_feature_reader_is_read_day_without_later_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sessions = make_store(directory)
            day = sessions[85]
            with MarketStore(path) as store:
                research = store.read_day(day)
                unbounded = store.read_feature_day(day)
            with MarketStore(path, through=day) as store:
                self.assertEqual(day, store.sessions[-1])
                bounded = store.read_feature_day(day)
            self.assertTrue(any(bar.session > day for bar in research.bars) and research.outcome_prices)
            self.assertEqual(without_later_records(research, day), bounded)
            self.assertEqual(bounded, unbounded)
            self.assertEqual(((), ()), (bounded.outcome_prices, bounded.exit_references))
            spec = ResearchSpec(UNIVERSE_ID)
            self.assertEqual(build_panel(research, spec), build_panel(bounded, spec))

    def test_physically_deleted_later_rows_leave_scores_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sessions = make_store(directory)
            day = sessions[85]
            copy = path.with_name("scoring-date-only.sqlite")
            shutil.copyfile(path, copy)
            removed = delete_later_rows(copy, day)
            self.assertTrue(removed["bars"] and removed["outcome_prices"] and removed["universes"] and removed["sessions"])
            original, deleted = score_day(path, day, StubModel()), score_day(copy, day, StubModel())
            self.assertEqual(original, deleted)
            self.assertEqual(len(CODES), len(set(original.ranks)))
            as_of = original.panel.rows[0].key.as_of
            signals = [build_signals(scored.predictions, scored.panel, as_ofs=(as_of,), model=StubModel())
                       for scored in (original, deleted)]
            self.assertEqual(signals[0], signals[1])
            self.assertEqual({original.panel.dataset_id, inference_input_id(original.panel, as_of)},
                             {s["inference_input_id"] for s in signals[0]})

    def test_scores_match_the_research_panel_and_signal_ranks(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sessions = make_store(directory)
            day, model = sessions[85], StubModel()
            scored = score_day(path, day, model)
            with MarketStore(path) as store:
                research_panel = build_panel(store.read_day(day), ResearchSpec(UNIVERSE_ID))
            self.assertEqual(research_panel, scored.panel)
            self.assertEqual(model.predict(research_panel.rows), scored.predictions)
            signals = build_signals(scored.predictions, scored.panel, as_ofs=(scored.panel.rows[0].key.as_of,),
                                    model=model)
            self.assertEqual(scored.ranks, tuple(s["cross_sectional_model_rank"] for s in signals))

    def test_bounded_store_refuses_outcomes_later_dates_and_later_trained_models(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sessions = make_store(directory)
            day = sessions[85]
            with MarketStore(path, through=day) as store:
                for read in (store.read_day, store.read_label_day):
                    with self.assertRaisesRegex(ContractError, "FEATURE_ONLY_STORE_HAS_NO_OUTCOMES"):
                        read(day)
                with self.assertRaisesRegex(ContractError, "SCORING_SESSION_NOT_IN_CALENDAR"):
                    store.read_feature_day(sessions[86])
            for through, reason in [(sessions[-1] + timedelta(days=1), "FEATURE_BOUND_NOT_A_SESSION"),
                                    (datetime(2020, 3, 26, tzinfo=CHINA), "FEATURE_BOUND_DATE_REQUIRED")]:
                with self.subTest(reason=reason), self.assertRaisesRegex(ContractError, reason):
                    MarketStore(path, through=through)
            as_of = score_day(path, day, StubModel()).panel.rows[0].key.as_of
            with self.assertRaisesRegex(ContractError, "MODEL_INFORMATION_AFTER_SCORING_TIME"):
                score_day(path, day, StubModel(training_cutoff=as_of))


if __name__ == "__main__":
    unittest.main()
