from dataclasses import replace
from datetime import date,datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import csv
import io
import tempfile
import unittest

from finauditgate import FinAuditGate,FrozenDocumentPackage
from finauditgate.cashflow import CashflowTask
from finauditgate.research import SecurityMarketTask
from finauditgate.adapters.model_budget import ModelBudget


def synthetic_market_task(*,as_of=date(2026,3,2),session=None):
    session=session or as_of
    tags = [('EntityCentralIndexKey','issuer','0001111111'),('EntityRegistrantName','issuer','Aurora Inc.'),
            ('Security12bTitle','ads','American Depositary Shares, each representing five ordinary shares'),
            ('TradingSymbol','ads','AURORA'),('SecurityExchangeName','ads','Nasdaq Stock Market LLC')]
    data=('<html><body>'+''.join(f'<ix:nonNumeric name="dei:{name}" contextRef="{ctx}">{value}</ix:nonNumeric>' for name,ctx,value in tags)+'</body></html>').encode()
    filing=CashflowTask('synthetic-identity',FrozenDocumentPackage('Aurora Inc.','identity.html',data,date(2026,1,1)),
        'https://www.sec.gov/Archives/edgar/data/1111111/000111111126000001/synthetic.htm',
        '0001111111-26-000001','1111111',date(2025,12,31),date(2024,12,31),as_of,'USD')
    fields={'Open':'20','High':'21','Low':'19','Close':'20.5','Volume':'1000','close_10_ema':'20','close_50_sma':'20','close_200_sma':'20',
            'rsi':'50','boll':'20','boll_ub':'21','boll_lb':'19','macd':'0','macds':'0','macdh':'0','atr':'1'}
    snapshot=f'## Verified market data snapshot for AURORA\n- Requested analysis date: {as_of}\n- Latest trading row used: {session}\n'+''.join(f'| {k} | {v} |\n' for k,v in fields.items())
    dates=[];d=session
    while len(dates)<205:
        if d.weekday()<5:dates.append(d)
        d-=timedelta(days=1)
    text=io.StringIO();writer=csv.writer(text);writer.writerow(['Date','Open','High','Low','Close','Volume'])
    for d in reversed(dates):writer.writerow([d.isoformat(),20,21,19,20.5,1000])
    quote={'symbol':'AURORA','longName':'Aurora Inc.','currency':'USD','exchange':'NMS','quoteType':'EQUITY',
        'regularMarketTime':int(datetime(session.year,session.month,session.day,16,tzinfo=ZoneInfo('America/New_York')).timestamp()),'regularMarketPrice':20.5}
    return SecurityMarketTask(filing,'AURORA',quote,snapshot,text.getvalue().encode(),session,
        'https://www.nasdaq.com/market-activity/stock-market-holiday-schedule')


class SecurityMarketTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.workspace=Path(tmp.name)
        (self.workspace/'finaudit-gate/.git').mkdir(parents=True);(self.workspace/'private').mkdir()
        self.root=self.workspace/'private/core';self.gate=FinAuditGate(artifact_root=self.root);self.task=synthetic_market_task()

    def test_identity_ratio_market_and_offline_replay(self):
        result=self.gate.run(self.task)
        self.assertEqual(5,result.report['identity']['ordinary_shares_per_ads'])
        self.assertEqual('20.5',result.report['market']['values']['Close'])
        self.assertTrue(self.gate.replay(result.run_ref).consistent)

    def test_wrong_security_context_does_not_borrow_another_class(self):
        f=self.task.identity_filing;doc=replace(f.document,document_bytes=f.document.document_bytes.replace(b'contextRef="ads">AURORA',b'contextRef="ordinary">AURORA'))
        with self.assertRaisesRegex(ValueError,'CONTEXT_AMBIGUOUS'):
            self.gate.run(replace(self.task,identity_filing=replace(f,document=doc)))

    def test_quote_currency_identity_and_session_conflicts(self):
        for key,value,code in [('currency','CNY','QUOTE_IDENTITY'),('longName','Another Company','QUOTE_IDENTITY'),
                               ('regularMarketPrice',25,'QUOTE_PRICE_TIME')]:
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,code):
                self.gate.run(replace(self.task,quote_metadata={**self.task.quote_metadata,key:value}))

    def test_snapshot_price_or_indicator_missing_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'SNAPSHOT_BAR_CONFLICT'):
            self.gate.run(replace(self.task,snapshot=self.task.snapshot.replace('| Close | 20.5 |','| Close | 25 |')))
        with self.assertRaisesRegex(ValueError,'FIELDS_MISSING'):
            self.gate.run(replace(self.task,snapshot=self.task.snapshot.replace('| rsi | 50 |\n','')))
        with self.assertRaisesRegex(ValueError,'DUPLICATE_FIELD'):
            self.gate.run(replace(self.task,snapshot=self.task.snapshot+'| Close | 20.5 |\n'))

    def test_market_financial_currency_mismatch_rejects_before_execution(self):
        from finauditgate.research import RunAuditedNativeResearch
        with self.assertRaisesRegex(ValueError,'NATIVE_SECURITY_TASK_MISMATCH'):
            RunAuditedNativeResearch(replace(self.task.identity_filing,currency='CNY'),
                self.task.symbol,'Synthetic research',market_task=self.task)

    def test_future_rows_and_blob_tampering_fail(self):
        with self.assertRaisesRegex(ValueError,'MARKET_BAR_INVALID'):
            self.gate.run(replace(self.task,ohlcv_csv=self.task.ohlcv_csv+b'2026-03-03,20,21,19,20.5,1000\n'))
        result=self.gate.run(self.task)
        p=self.root/'blobs/sha256'/result.report['task']['ohlcv_sha256'];p.write_bytes(b'changed')
        self.assertFalse(self.gate.replay(result.run_ref).consistent)

    def test_uncapped_spend_still_has_operational_limits(self):
        budget=ModelBudget(ceiling_cny=None,input_per_million='999999999',max_calls=1,max_input_bytes=100,max_output_tokens=32768)
        budget.reserve('A legitimate bounded request')
        self.assertIsNone(budget.receipt()['ceiling_cny'])
        with self.assertRaisesRegex(ValueError,'CALL_LIMIT'):budget.reserve('again')
        with self.assertRaisesRegex(ValueError,'INPUT_LIMIT'):ModelBudget(ceiling_cny=None,max_input_bytes=1).reserve('xx')


if __name__=='__main__':unittest.main()
