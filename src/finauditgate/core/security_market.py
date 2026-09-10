"""Source-bound ADS identity and deterministic market snapshot checks."""

import csv
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from finauditgate.cashflow import CashflowOutcome
from finauditgate.contracts import Decision, RunRef, ReplayReport
from finauditgate.core import cashflow
from finauditgate.core.ixbrl import Filing
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.research import SecurityMarketTask

SCHEMA = "finauditgate.security-market/v1"


def snapshot_fields(text):
    symbol = re.search(r"^## Verified market data snapshot for (\S+)", text)
    requested = re.search(r"Requested analysis date: (\d{4}-\d{2}-\d{2})", text)
    latest = re.search(r"Latest trading row used: (\d{4}-\d{2}-\d{2})", text)
    if not all((symbol, requested, latest)):
        raise ValueError("MARKET_SNAPSHOT_HEADER_INVALID")
    pairs = re.findall(r"(?m)^\| (Open|High|Low|Close|Volume|close_10_ema|close_50_sma|close_200_sma|rsi|boll|boll_ub|boll_lb|macd|macds|macdh|atr) \| ([^|]+?) \|$", text)
    fields = dict(pairs)
    if len(pairs) != len(fields):
        raise ValueError("MARKET_SNAPSHOT_DUPLICATE_FIELD")
    required = {"Open","High","Low","Close","Volume","close_10_ema","close_50_sma","close_200_sma","rsi","boll","boll_ub","boll_lb","macd","macds","macdh","atr"}
    if set(fields) != required:
        raise ValueError("MARKET_SNAPSHOT_FIELDS_MISSING")
    try:
        values = {k: Decimal(v.strip()) for k,v in fields.items()}
        if not all(v.is_finite() for v in values.values()) or not 0 <= values['rsi'] <= 100 or values['atr'] < 0:
            raise ValueError("MARKET_SNAPSHOT_VALUE_INVALID")
    except ArithmeticError as exc:
        raise ValueError("MARKET_SNAPSHOT_VALUE_INVALID") from exc
    return {'symbol':symbol[1], 'as_of':requested[1], 'latest_session':latest[1], 'values':{k:str(v) for k,v in values.items()}}


def _record(task):
    source = task.identity_filing
    if source.document.declared_published_at > source.cutoff:
        raise ValueError("POST_CUTOFF_IDENTITY_SOURCE")
    filing = Filing(source.document.document_bytes)
    tags = [n for n in filing.nodes if n.tag == 'ix:nonnumeric']
    def named(name):
        return [n for n in tags if n.attrs.get('name','').split(':')[-1] == name]
    cik = {int(n.text().strip()) for n in named('EntityCentralIndexKey')}
    if cik != {int(source.entity_identifier)}:
        raise ValueError("SECURITY_CIK_SOURCE_CONFLICT")
    symbols = [n for n in named('TradingSymbol') if n.text().strip() == task.symbol]
    if len(symbols) != 1:
        raise ValueError("SECURITY_SYMBOL_SOURCE_REQUIRED")
    symbol = symbols[0]; context = symbol.attrs.get('contextref')
    titles = [n for n in named('Security12bTitle') if n.attrs.get('contextref') == context]
    exchanges = [n for n in named('SecurityExchangeName') if n.attrs.get('contextref') == context]
    issuers = named('EntityRegistrantName')
    if len(titles) != 1 or len(exchanges) != 1 or not issuers:
        raise ValueError("SECURITY_CLASS_CONTEXT_AMBIGUOUS")
    title = ' '.join(titles[0].text().split()); exchange = exchanges[0].text().strip()
    ratio = re.search(r'American Depositary Shares, each representing (\w+) ordinary shares', title, re.I)
    words = {'one':1,'two':2,'three':3,'four':4,'five':5,'ten':10,'twenty':20,'twentyfive':25}
    multiplier = (int(ratio[1]) if ratio and ratio[1].isdigit() else words.get(ratio[1].lower()) if ratio else None)
    if multiplier is None or not 1 <= multiplier <= 1000 or 'nasdaq' not in exchange.lower():
        raise ValueError("SUPPORTED_ADS_IDENTITY_REQUIRED")
    quote = task.quote_metadata
    if set(quote) - {'symbol','longName','shortName','currency','exchange','fullExchangeName','quoteType','regularMarketTime','regularMarketPrice'}:
        raise ValueError("QUOTE_METADATA_FIELDS_NOT_ALLOWED")
    clean = lambda text: re.sub(r'[^a-z0-9]','',text.lower())
    if (quote.get('symbol') != task.symbol or quote.get('currency') != 'USD'
            or quote.get('exchange') not in ('NMS','NGM','NCM') or quote.get('quoteType') != 'EQUITY'
            or clean(quote.get('longName','')) != clean(issuers[0].text())):
        raise ValueError("QUOTE_IDENTITY_MISMATCH")
    snapshot = snapshot_fields(task.snapshot)
    if (snapshot['symbol'] != task.symbol or snapshot['as_of'] != source.cutoff.isoformat()
            or snapshot['latest_session'] != task.expected_session.isoformat()
            or task.expected_session.weekday() >= 5 or (source.cutoff-task.expected_session).days > 10):
        raise ValueError("MARKET_SESSION_MISMATCH")
    rows = list(csv.DictReader(io.StringIO(task.ohlcv_csv.decode('utf-8'))))
    if not 200 <= len(rows) <= 10000:
        raise ValueError("MARKET_HISTORY_DEPTH_UNSUPPORTED")
    if not {'Date','Open','High','Low','Close','Volume'} <= set(rows[0]):
        raise ValueError('MARKET_BAR_COLUMNS_MISSING')
    seen = []
    for row in rows:
        d = date.fromisoformat(row['Date'][:10]); seen.append(d)
        prices = [Decimal(row[k]) for k in ('Open','High','Low','Close')]
        if (d > source.cutoff or not all(x.is_finite() and x>0 for x in prices)
                or prices[1] < max(prices[0],prices[2],prices[3]) or prices[2] > min(prices[0],prices[1],prices[3])
                or not Decimal(row['Volume']).is_finite() or Decimal(row['Volume']) < 0):
            raise ValueError("MARKET_BAR_INVALID")
    if seen != sorted(set(seen)) or seen[-1] != task.expected_session:
        raise ValueError("MARKET_BAR_DATES_INVALID")
    for key in ('Open','High','Low','Close','Volume'):
        tolerance = Decimal(0) if key == 'Volume' else Decimal('0.005')
        if abs(Decimal(rows[-1][key])-Decimal(snapshot['values'][key])) > tolerance:
            raise ValueError("MARKET_SNAPSHOT_BAR_CONFLICT")
    quote_day = datetime.fromtimestamp(quote['regularMarketTime'], ZoneInfo('America/New_York')).date()
    if quote_day != task.expected_session or abs(Decimal(str(quote['regularMarketPrice']))-Decimal(snapshot['values']['Close'])) > Decimal('0.005'):
        raise ValueError("QUOTE_PRICE_TIME_CONFLICT")
    payload = {'identity_task':cashflow.task_payload(source),'symbol':task.symbol,'quote_metadata':quote,
        'snapshot_sha256':sha256_hex(task.snapshot.encode()),'ohlcv_sha256':sha256_hex(task.ohlcv_csv),
        'expected_session':task.expected_session.isoformat(),'calendar_source':task.calendar_source}
    return {'schema_version':SCHEMA,'task':payload,'identity':{'symbol':task.symbol,'entity_identifier':str(int(source.entity_identifier)),
        'issuer':issuers[0].text().strip(),'security_type':'ADS','ordinary_shares_per_ads':multiplier,'exchange':exchange,
        'quote_currency':'USD','financial_currency':source.currency,'source_url':source.source_url,
        'references':[filing.reference(n) for n in (symbol,titles[0],exchanges[0],issuers[0])]},
        'market':{**snapshot,'history_rows':len(rows),'price_basis':'YFINANCE_AUTO_ADJUSTED_OHLCV',
            'vendor':'Yahoo Finance via pinned TradingAgents','snapshot':task.snapshot},
        'limits':['Calendar session is supplied from the cited market calendar, not a general exchange-calendar engine.',
                  'Indicators are upstream computations on the frozen processed OHLCV; this is not independent exchange-data reconciliation.',
                  'ADS quote prices must not be compared directly with CNY per-ordinary-share earnings; no FX or valuation conversion has been performed.'],
        'decision':'HUMAN_REVIEW'}


def run(task, root):
    try:
        record = _record(task)
    except ArithmeticError as exc:
        raise ValueError('SECURITY_MARKET_NUMERIC_INVALID') from exc
    raw = canonical_json_bytes(record); ref = RunRef(sha256_hex(raw))
    for data in (task.identity_filing.document.document_bytes,task.snapshot.encode(),task.ohlcv_csv):
        write_once(root/'blobs/sha256'/sha256_hex(data),data)
    write_once(root/'runs'/ref.run_id/'security-market.json',raw)
    return CashflowOutcome(ref,Decision.HUMAN_REVIEW,record['task']['identity_task']['document']['document_sha256'],record)


def read_record(root,ref):
    raw=(root/'runs'/ref.run_id/'security-market.json').read_bytes();record=json.loads(raw)
    if len(raw)>1024*1024 or sha256_hex(raw)!=ref.run_id or canonical_json_bytes(record)!=raw or record.get('schema_version')!=SCHEMA:
        raise ValueError('SECURITY_MARKET_RECORD_INVALID')
    return record


def replay(root,ref):
    try:
        record=read_record(root,ref);p=record['task'];blobs=root/'blobs/sha256'
        filing=cashflow._task_from_payload(p['identity_task'],(blobs/p['identity_task']['document']['document_sha256']).read_bytes())
        snapshot=(blobs/p['snapshot_sha256']).read_bytes();bars=(blobs/p['ohlcv_sha256']).read_bytes()
        if sha256_hex(snapshot)!=p['snapshot_sha256'] or sha256_hex(bars)!=p['ohlcv_sha256']:
            raise ValueError('SECURITY_MARKET_BLOB_HASH_MISMATCH')
        task=SecurityMarketTask(filing,p['symbol'],p['quote_metadata'],snapshot.decode(),bars,
            date.fromisoformat(p['expected_session']),p['calendar_source'])
        if _record(task)!=record:raise ValueError('SECURITY_MARKET_REPLAY_MISMATCH')
        return ReplayReport('finauditgate.security-market-replay/v1',ref,True,Decision.HUMAN_REVIEW,None,None,4)
    except (ValueError,TypeError,KeyError,OSError,ArithmeticError) as exc:
        return ReplayReport('finauditgate.security-market-replay/v1',ref,False,None,None,None,0,str(exc))
