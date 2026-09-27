"""Number contract 2 (Case v18) accepts only listed time and source labels.

Every sentence is synthetic. Contract 2 only removes labels from contract 1's
pending list, so it can never turn a refusal into a pass or add a pending item.
"""

import itertools
import unittest

from test_research_delivery import pending_numbers, report
from test_research_numbers import inputs
from finauditgate.application.research_delivery import (
    DeliveryContext, contract_for, normalize_report, report_context,
)
from finauditgate.core.artifacts import sha256_hex


def bundle(*, indicators=True):
    rows = {"S01": "SYNTHETIC filing text." + (" SYNTHETIC market table: close_10_ema 10 EMA, close_50_sma 50 SMA, 200 SMA." if indicators else ""),
            "QUANT": "SYNTHETIC_QUANT：横截面相对排序，预测期限为20个交易日；不是收益预测或上涨概率。",
            **{sid: "SYNTHETIC event index " + sid for sid in (
                "AUTO_NEWS01", "AUTO_NEWS02", "AUTO_SOCIAL01", "AUTO_SOCIAL02", "AUTO_SOCIAL03", "S07", "S08", "S09")}}
    return {"schema_version": "finresearchops.thesis-sources/v2", "sources": [
        {"id": sid, "content": body, "sha256": sha256_hex(body.encode()), "use": "research"} for sid, body in rows.items()]}


def pending(text, contract, sources=None):
    sources = sources or bundle()
    value = normalize_report(report(text), sources)
    return pending_numbers(report_context(value, *inputs(), sources,
        {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12},
        changes=[], beliefs=[], contract=contract))


class NumberContractV2Test(unittest.TestCase):
    def test_canonicalized_time_and_source_labels_are_no_longer_pending(self):
        cases = (
            ("无2025全年、2026、2027的量化指引。", ["2025", "2026", "2027"]),
            ("经营改善。2025前三季度的非经常性损益仍需核对。", ["2025"]),
            ("2025上半年的收入增长仍需核对。", ["2025"]),
            ("2025三季报未经审计。", ["2025"]),
            ("缺少公司2025全年指引。", ["2025"]),
            ("价格与利润持续性；未解决的是2025Q4与2026的价格。", ["2025Q4", "2026"]),
            ("历史H1数据支持这一机制，但H2仍需观察。", ["H1", "H2"]),
            ("QUANT仅20日排序，不代表年度收益。", ["20"]),
            ("QUANT 20日横截面排序偏低。", ["20"]),
            ("AUTO_NEWS01/02为活动索引，AUTO_SOCIAL01-03为问答。", ["02", "03"]),
            ("价格低于10EMA、50SMA和200SMA。", ["10EMA", "50SMA", "200SMA"]),
        )
        for text, before in cases:
            with self.subTest(text=text):
                self.assertEqual(before, pending(text, 1))
                self.assertEqual([], pending(text, 2))

    def test_quantities_near_labels_stay_visible_as_pending(self):
        cases = (
            ("出货量达2030全年。", ["2030"]),
            ("销量为2050上半年。", ["2050"]),
            ("出货量2030、2050上半年。", ["2030", "2050"]),
            ("详见S07-09页。", ["09"]),
            ("QUANT 20日均线向下。", ["20"]),
            ("QUANT仅5日排序。", ["5"]),
            ("AUTO_NEWS01/05为索引。", ["05"]),
            ("价格低于10EMA。", ["10EMA"]),
            ("H3与2025H3仍需核对。", ["H3", "2025H3"]),
            ("9M扣非仍下降。", ["9M"]),
            ("USD5的换算仍需核对。", ["USD5"]),
            ("收入为AUTO_NEWS01/02。", ["02"]),
            ("若2025Q4和2026延续改善，情景才成立。", ["2026"]),
            ("资料未给2025Q4及2026价格指引。", ["2026"]),
            ("2025年与2050 mn美元的回购安排仍需核对。", ["2050"]),
            ("公司2024年和2000多家门店的数据尚未披露。", ["2000"]),
            ("销量,2050上半年仍需核对。", ["2050"]),
        )
        for text, tokens in cases:
            sources = bundle(indicators=False) if "10EMA" in text else None
            with self.subTest(text=text):
                self.assertEqual(tokens, pending(text, 1, sources))
                self.assertEqual(tokens, pending(text, 2, sources))

    def test_a_quantity_after_a_label_list_stays_pending(self):
        for text, label, quantity in (("无2025全年、2030台的出货指引。", "2025", "2030"),
                                      ("无2025全年与2000 mn的对价说明。", "2025", "2000"),
                                      ("缺少2025全年和2000欧元订单的数据。", "2025", "2000"),
                                      ("无2025上半年与2050港币的说明。", "2025", "2050"),
                                      ("H1、2000余台设备已投产。", "H1", "2000")):
            with self.subTest(text=text):
                self.assertEqual([label, quantity], pending(text, 1))
                self.assertEqual([quantity], pending(text, 2))

    def test_contract_2_only_removes_labels_from_contract_1(self):
        lefts = ("", "无", "公司", "销量为", "出货量达", "收入,", "单价")
        labels = ("", "2025全年", "2025年", "2025Q4", "H1", "FY2025")
        rights = ("全年", "上半年", "的", "、", "价格", "延续", "多家", "余台", "左右", " mn", " bn", "欧元", "港币",
                  "兆瓦", "笔", "艘", "手", "份", "台", "万元", "亿", "%")
        for left, label, join, right in itertools.product(lefts, labels, ("", "和", "、", "至"), rights):
            if bool(label) != bool(join):
                continue
            text = f"{left}{label}{join}2050{right}的情况仍需核对。"
            try:
                first = pending(text, 1)
            except ValueError:
                with self.assertRaises(ValueError, msg=text):
                    pending(text, 2)
                continue
            second = pending(text, 2)
            self.assertTrue(set(second) <= set(first), text)
            if "2050" in first and not right.startswith(("全年", "上半年", "的", "、")):
                self.assertIn("2050", second, text)

    def test_value_positions_are_still_refused(self):
        for text in ("收入为2025Q4。", "EPS为2026。", "2030全年12万片。", "QUANT 20日收益为8%。",
                     "价格为10EMA。", "利润率2025H1%。", "股价跌破10EMA之后的短期支撑位为50。",
                     "股价跌破50SMA之后的短期支撑位约48。", "利润在QUANT 20日横截面相对排序中为50。",
                     "价格S01:10EMA。", "收益为S01 QUANT 20日。"):
            for contract in (1, 2):
                with self.subTest(text=text, contract=contract), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                    pending(text, contract)

    def test_contract_follows_the_saved_case_version(self):
        self.assertEqual(2, contract_for({"schema_version": "finresearchops.thesis-case/v18"}))
        for version in ("finresearchops.thesis-case/v16", "finresearchops.thesis-case/v17"):
            self.assertEqual(1, contract_for({"schema_version": version}))
        with self.assertRaisesRegex(ValueError, "RESEARCH_NUMBER_CONTRACT_INVALID"):
            DeliveryContext(*inputs(), bundle(), report(), {"symbol": "AURORA", "as_of": "2026-12-31",
                                                             "horizon_months": 12}, contract=3)


if __name__ == "__main__":
    unittest.main()
