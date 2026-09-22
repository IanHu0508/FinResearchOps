from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import sqlite3
import tempfile
import unittest

from quant.contracts import ResearchSpec, market_time
from quant.data.acquisition import read_cached
from quant.data.market_store import MarketStore, build_store, UNIVERSE_ID
from quant.data.serialization import from_document, to_document
from quant.data.identity import IdentityMap
from quant.data.share_conversions import build_exit_references, HOLDER_CONVENTION
from quant.labels.forward import build_labels
from quant.pipeline import prepare_dataset
from quant.splits.walk_forward import FoldWindow, partition_label
from quant.tests.test_acquisition import frame
from quant.tests import test_market_store as market_store_fixtures


def change_synthetic_day(raw_root, day, changes):
    """Mutate only a newly generated temporary fixture, never a provider cache."""
    name = "daily-" + str(day)
    table = read_cached(raw_root, name, method="query_daily_history_k_AStock", row_index=4, field_index=5)
    rows = [list(row) for row in table.rows]
    for field, value in changes.items():
        rows[0][table.fields.index(field)] = value
    wire = frame(["0", "ok", "query_daily_history_k_AStock", "anonymous",
                  json.dumps({"record": rows}), ",".join(table.fields)], "99")
    (raw_root / (name+".bin")).write_bytes(wire)
    path = raw_root / (name+".json")
    meta = json.loads(path.read_text())
    meta["sha256"] = hashlib.sha256(wire).hexdigest()
    meta["wire_bytes"] = len(wire)
    path.write_text(json.dumps(meta))


def add_synthetic_price_change_field(raw_root, sessions):
    """Give the generic fixture the full provider projection's pctChg field."""
    for day in sessions:
        name = "daily-" + str(day)
        table = read_cached(raw_root, name, method="query_daily_history_k_AStock", row_index=4, field_index=5)
        fields = (*table.fields, "pctChg")
        rows = [list(row) + [str((float(row[table.fields.index("close")]) /
                                 float(row[table.fields.index("preclose")]) - 1) * 100)] for row in table.rows]
        wire = frame(["0", "ok", "query_daily_history_k_AStock", "anonymous",
                      json.dumps({"record": rows}), ",".join(fields)], "99")
        (raw_root / (name+".bin")).write_bytes(wire)
        path = raw_root / (name+".json")
        meta = json.loads(path.read_text())
        meta.update(sha256=hashlib.sha256(wire).hexdigest(), wire_bytes=len(wire))
        path.write_text(json.dumps(meta))


class OutcomeIntegrationTests(unittest.TestCase):
    def test_amount_alias_conflict_does_not_erase_agreed_future_price(self):
        from quant.tests.test_identity import identity_map
        panels, labels = [], []
        for conflict in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                raw, target, sessions = market_store_fixtures.MarketStoreTests().make_raw(
                    directory, rename_at=90, alias_conflict_at=85 if conflict else None)
                add_synthetic_price_change_field(raw, sessions)
                build_store(raw, target, identity_map=identity_map(sessions[90]))
                with MarketStore(target) as store:
                    dataset = prepare_dataset(store.read_day(sessions[80]), ResearchSpec(UNIVERSE_ID))
                    self.assertEqual(2, len(dataset.labels))
                    self.assertTrue(all(y.complete for y in dataset.labels))
                    if conflict:
                        # The amount stays disputed and the feature chain still
                        # has a gap; only the agreed label-price path survives.
                        self.assertEqual(("IDENTITY_QUOTE_CONFLICT:amount",), store.connection.execute(
                            "SELECT reason FROM issues WHERE session=?", (str(sessions[85]),)).fetchone())
                        self.assertEqual(0, store.connection.execute(
                            "SELECT COUNT(*) FROM bars WHERE security_id='security:example-a' AND session=?",
                            (str(sessions[85]),)).fetchone()[0])
                        eligible, streak = store.connection.execute(
                            "SELECT eligible,consecutive_sessions FROM bars "
                            "WHERE security_id='security:example-a' AND session=?",
                            (str(sessions[86]),)).fetchone()
                        self.assertEqual((0, 1), (eligible, streak))
                        price, chain, ready = store.connection.execute(
                            "SELECT close,chain_start,available_at FROM outcome_prices "
                            "WHERE security_id='security:example-a' AND session=?",
                            (str(sessions[85]),)).fetchone()
                        self.assertEqual(10.85, price)
                        self.assertEqual(str(sessions[0]), chain)
                        self.assertEqual(market_time(sessions[85], 21).isoformat(), ready)
                    panels.append(dataset.panel.rows)
                    labels.append(dataset.labels)
        self.assertEqual(panels[0], panels[1])
        for clean, disputed in zip(labels[0], labels[1]):
            self.assertEqual((clean.entry_date, clean.label_end_date, clean.available_at,
                              clean.raw_return, clean.target_percentile),
                             (disputed.entry_date, disputed.label_end_date, disputed.available_at,
                              disputed.raw_return, disputed.target_percentile))

    def test_conversion_reference_inherits_late_price_anchor_evidence(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE sessions(ordinal INTEGER, session TEXT);
                CREATE TABLE outcome_prices(security_id TEXT,session TEXT,factor REAL,chain_start TEXT,
                    source_id TEXT,return_close REAL,available_at TEXT);
                CREATE TABLE outcome_exit_references(security_id TEXT,session TEXT,available_at TEXT,
                    return_close REAL,source_id TEXT,anchor_chain_start TEXT,method_id TEXT);
            """)
            connection.execute("INSERT INTO sessions VALUES(0,'2020-01-03')")
            connection.executemany("INSERT INTO outcome_prices VALUES(?,?,?,?,?,?,?)", [
                ("ticker:600001.SH", "2020-01-01", 2, "2020-01-01", "old", 20, "2020-01-20T21:00:00+08:00"),
                ("ticker:600002.SH", "2020-01-03", 5, "2020-01-03", "new", 50, "2020-01-12T21:00:00+08:00"),
            ])
            document = {"schema_version": "quant.verified-share-conversions/v1", "holder_convention": HOLDER_CONVENTION,
                "events": [{"old_symbol": "600001.SH", "successor_symbol": "600002.SH", "effective_date": "2020-01-02",
                    "first_reference_date": "2020-01-03", "last_reference_date": "2020-01-03", "old_anchor_date": "2020-01-01",
                    "successor_anchor_date": "2020-01-03", "ratio": "0.5", "confirmation_date": "2020-01-05", "evidence_ids": ["synthetic"]}]}
            build_exit_references(connection, document, IdentityMap(), bar_table="outcome_prices", target_table="outcome_exit_references")
            ready, value = connection.execute("SELECT available_at,return_close FROM outcome_exit_references").fetchone()
            self.assertEqual("2020-01-20T21:00:00+08:00", ready)
            self.assertEqual(10, value)
        finally:
            connection.close()

    def test_missing_future_activity_recovers_target_without_changing_history(self):
        panels, labels = [], []
        for missing in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                raw, target, sessions = market_store_fixtures.MarketStoreTests().make_raw(directory, halt_missing_at=90)
                if not missing:
                    change_synthetic_day(raw, sessions[90], {"volume": "0", "amount": "0"})
                build_store(raw, target)
                with MarketStore(target) as store:
                    data = store.read_day(sessions[80])
                    dataset = prepare_dataset(data, ResearchSpec(UNIVERSE_ID))
                    self.assertEqual(2, len(dataset.panel.rows))
                    self.assertTrue(all(y.complete for y in dataset.labels))
                    self.assertEqual(0 if missing else 1, store.connection.execute(
                        "SELECT COUNT(*) FROM bars WHERE security_id='ticker:600001.SH' AND session=?",
                        (str(sessions[90]),)).fetchone()[0])
                    self.assertEqual(data, from_document(to_document(data)))
                    if missing:
                        self.assertEqual((0,), store.connection.execute(
                            "SELECT eligible FROM bars WHERE security_id='ticker:600001.SH' AND session=?",
                            (str(sessions[91]),)).fetchone())
                panels.append(dataset.panel.rows)
                labels.append(dataset.labels)
        self.assertEqual(panels[0], panels[1])
        for a, b in zip(labels[0], labels[1]):
            self.assertEqual((a.entry_date, a.label_end_date, a.target_percentile),
                             (b.entry_date, b.label_end_date, b.target_percentile))
            self.assertAlmostEqual(a.raw_return, b.raw_return, places=12)

    def make_delayed(self, directory, *, confirmed=True):
        raw, target, sessions = market_store_fixtures.MarketStoreTests().make_raw(directory, session_count=140)
        price = str(10 + 89/100)
        for pos in range(90, 110 if confirmed else 140):
            changes = {k: price for k in ("open", "high", "low", "close", "preclose")}
            changes.update(volume="" if pos == 90 else "0", amount="" if pos == 90 else "0", tradestatus="0")
            change_synthetic_day(raw, sessions[pos], changes)
        if confirmed:
            change_synthetic_day(raw, sessions[110], {"preclose": price})
        build_store(raw, target)
        return target, sessions

    def test_pending_dependency_is_censored_before_rank_identification(self):
        with tempfile.TemporaryDirectory() as directory:
            target, sessions = self.make_delayed(directory)
            with MarketStore(target) as store:
                data = store.read_day(sessions[80])
                dataset = prepare_dataset(data, ResearchSpec(UNIVERSE_ID))
            self.assertTrue(all(y.complete for y in dataset.labels))
            self.assertEqual({market_time(sessions[110], 21)}, {y.available_at for y in dataset.labels})
            self.assertEqual({sessions[100]}, {y.label_end_date for y in dataset.labels})
            window = FoldWindow(sessions[60], sessions[105], sessions[115], sessions[120], sessions[130])
            from quant.labels.forward import labels_at_cutoff
            view=labels_at_cutoff(dataset,market_time(sessions[105],0))
            self.assertEqual(1,sum(y.supervised for y in view.labels))
            self.assertEqual({("train","OUTCOME_UNAVAILABLE"),("train","AVAILABLE")},
                             {partition_label(y,window) for y in view.labels})
            self.assertTrue(all(y.target_percentile is None for y in view.labels))
            # Changing a price's declared basis cannot borrow the feature-bar
            # endpoint or rank only the unaffected survivor.
            bad_prices = tuple(replace(q, basis_id="wrong-basis") if q.symbol == "600001.SH"
                and q.session == sessions[100] else q for q in data.outcome_prices)
            bad = build_labels(replace(data, outcome_prices=bad_prices), dataset.panel)
            self.assertTrue(all(not y.complete for y in bad.labels))
            self.assertTrue(all(y.target_percentile is None for y in bad.labels))

    def test_unconfirmed_tail_is_not_a_terminal_value(self):
        with tempfile.TemporaryDirectory() as directory:
            target, sessions = self.make_delayed(directory, confirmed=False)
            with MarketStore(target) as store:
                data = store.read_day(sessions[80])
                dataset = prepare_dataset(data, ResearchSpec(UNIVERSE_ID))
                self.assertEqual(0, store.connection.execute("SELECT COUNT(*) FROM outcome_prices "
                    "WHERE security_id='ticker:600001.SH' AND session>=?", (str(sessions[90]),)).fetchone()[0])
            self.assertEqual(2, len(dataset.labels))
            self.assertTrue(all(not y.complete for y in dataset.labels))
