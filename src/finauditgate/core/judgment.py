"""Bounded financial propositions; no approval of arbitrary model prose."""

import csv
import io
import json
import re
from dataclasses import replace
from decimal import Decimal,Context,ROUND_HALF_EVEN,localcontext

from finauditgate.cashflow import CashflowOutcome
from finauditgate.contracts import Decision, RunRef, ReplayReport
from finauditgate.research import ReviewNativeJudgment
from finauditgate.core import research_evidence, security_market
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once

SCHEMA = 'finauditgate.native-judgment/v1'
PRICE_CONTEXT=Context(prec=40,rounding=ROUND_HALF_EVEN)
NODES = ('catalog','Fundamentals Analyst','Market Analyst','Bull Researcher','Bear Researcher',
         'Research Manager','Trader','Aggressive Analyst','Conservative Analyst','Neutral Analyst','Portfolio Manager')
MANAGERS = {'Research Manager','Portfolio Manager'}
RELATIONS = ('VALUE','POSITIVE','NEGATIVE','INCREASED','DECREASED','TURNED_NEGATIVE',
             'UNKNOWN','ZERO','GENERATES_CASH','NO_NEW_CASH')
HYPOTHESES = {
    'OPERATING_SUSTAINABILITY': ('营业利润的持续性待验证','分部收入、毛利、费用及一次性项目', '后续同口径经营证据与所选方向不符', ['metric:operating_profit']),
    'CASH_QUALITY': ('经营现金创造的持续性待验证','本期现金收付与营运资金滚动勾稽','后续净现金流与构成不支持所选方向',['metric:operating_cashflow']),
    'CONTRACT_CAUSE': ('合同负债变化的业务原因待验证','毛收款、收入确认、退款、汇率及并购的滚动勾稽','新增证据排除所选方向的业务解释',['metric:contract_balance','metric:contract_adjustment']),
    'INVESTMENT_RECURRENCE': ('投资损益的重复性待验证','投资资产构成、损益性质与后续披露','后续同口径投资证据与所选方向不符',['metric:investment']),
    'PRICE_TREND': ('价格趋势延续或修复仅为待验证情景','更新的同字段复权行情及指标','更新行情未满足所选方向的观察条件',['PRICE_']),
}


def _catalog(root, financial, market):
    a, task = financial['analysis'],financial['task']
    catalog = {}
    def add(key,label,measure,current=None,comparison=None,**extra):
        catalog[key] = {'label':label,'measure':measure,'current':current,'comparison':comparison,**extra}
    for f in a['facts']:
        meaning = financial.get('reference_meanings',{}).get(f['fact_id'],{})
        add(f['fact_id'],meaning.get('label','报表项目')+' / '+f['row_label'],
            meaning.get('measure_kind','FINANCIAL_FACT'),f['value'],currency=f['currency'],
            current_end=f['period_end'],period_start=f['period_start'],source_url=task['source_url'])
    def metric(key,label,measure,value):
        if value:
            add('metric:'+key,label,measure,value['current'],value['comparison'],currency=a['currency'],
                current_end=value.get('current_end',task['current_end']),
                comparison_end=value.get('comparison_end',task['comparison_end']),fact_ids=value.get('fact_ids',[]))
    metric('consolidated_income','合并净利润','INCOME',a['metrics'].get('profit'))
    metric('operating_cashflow','经营现金流净额（不是毛收款）','NET_CASH_FLOW',a['metrics'].get('operating_cashflow'))
    metric('parent_income','归母净利润','PARENT_INCOME',(a.get('earnings_attribution') or {}).get('parent'))
    for key,item in (a.get('earnings_attribution') or {}).items():
        if key in ('noncontrolling_deduction','accretion_deduction'):
            metric(key,'合并至归母的带符号归属扣减（负号不表示亏损）','ATTRIBUTION_ADJUSTMENT',item)
    for item in (a.get('profit_bridge') or {}).get('components',[]):
        metric(item['key'],item['label']+'（利润表带符号项目）','INCOME_STATEMENT',item)
    for key,label in (('deferred_tax_expense','递延所得税费用（非现金流调节）'),
                      ('current_tax_expense','当期所得税费用'),('total_tax_expense','所得税费用合计'),
                      ('cash_income_taxes_paid','实付所得税净额')):
        metric(key,label,'CASH_PAYMENT' if key.startswith('cash_') else 'TAX_EXPENSE',a.get('supplemental',{}).get(key))
    metric('contract_balance','合同负债余额','BALANCE',a.get('supplemental',{}).get('contract_liability_balance'))
    for driver in a['drivers']:
        if driver['label'].lower() == 'contract liabilities':
            metric('contract_adjustment','合同负债现金流调节额（不是余额或毛收款）','RECONCILIATION',driver)
    bars=(root/'blobs/sha256'/market['task']['ohlcv_sha256']).read_bytes()
    for row in list(csv.DictReader(io.StringIO(bars.decode())))[-31:]:
        for field in ('Open','High','Low','Close','Volume'):
            label={'Open':'开盘价','High':'日内最高价','Low':'日内最低价','Close':'收盘价','Volume':'成交量'}[field]
            with localcontext(PRICE_CONTEXT):
                value=str(Decimal(row[field]).quantize(Decimal('1') if field=='Volume' else Decimal('.01')))
            add(f"quote:{row['Date'][:10]}:{field}",label,'PRICE_'+field.upper(),
                value,
                currency='ADS' if field=='Volume' else market['identity']['quote_currency']+'/ADS',
                current_end=row['Date'][:10],basis=market['market']['price_basis'])
    for field,value in market['market']['values'].items():
        if field not in ('Open','High','Low','Close','Volume'):
            add('indicator:'+field,field+'（上游计算，动态观察）','INDICATOR',value,
                current_end=market['market']['latest_session'],basis=market['market']['price_basis'])
    add('context:holdings','实际持仓','HOLDINGS','UNKNOWN')
    add('context:valuation','估值与预期收益依据','VALUATION','UNKNOWN')
    add('rule:revenue_recognition','确认过去已收的预收收入本身不会产生本期新现金','RECOGNITION','NO_NEW_CASH')
    return catalog


def _sources(root, task):
    if not research_evidence.replay(root,task.financial_ref).consistent or not security_market.replay(root,task.market_ref).consistent:
        raise ValueError('JUDGMENT_SOURCE_REPLAY_FAILED')
    f=research_evidence.read_record(root,task.financial_ref);m=security_market.read_record(root,task.market_ref)
    if not f['analysis']['metrics']:
        raise ValueError('JUDGMENT_FINANCIAL_INPUT_UNAVAILABLE')
    if (str(int(f['task']['entity_identifier']))!=m['identity']['entity_identifier']
            or f['task']['cutoff']!=m['market']['as_of'] or f['analysis']['currency']!=m['identity']['financial_currency']):
        raise ValueError('JUDGMENT_SOURCE_IDENTITY_MISMATCH')
    return _catalog(root,f,m)


def _claim(c, catalog):
    """Only these propositions are rendered; model wording is never approved."""
    if set(c)!= {'id','kind','refs','measure','relation','topic','stance'} or not re.fullmatch(r'c[1-9][0-9]?',str(c.get('id',''))):
        raise ValueError('JUDGMENT_CLAIM_SHAPE_INVALID')
    refs=c['refs']
    if type(refs) is not list or not 1<=len(refs)<=4 or any(type(r) is not str or r not in catalog for r in refs) or len(set(refs))!=len(refs):
        return False,'REFERENCE_NOT_AVAILABLE','未支持：来源引用不可用。'
    if c['kind']=='HYPOTHESIS':
        info=HYPOTHESES.get(c['topic'])
        if info is None or c['stance'] not in ('SUPPORT','CHALLENGE','OPEN') or c['measure']!='HYPOTHESIS' or c['relation']!='UNKNOWN':
            return False,'HYPOTHESIS_TYPE_INVALID','未支持：假设类型无效。'
        if not any(r in info[3] or any(catalog[r]['measure'].startswith(t) for t in info[3] if t.endswith('_')) for r in refs):
            return False,'HYPOTHESIS_EVIDENCE_MISMATCH','未支持：引用与假设主题不匹配。'
        direction={'SUPPORT':'支持方向','CHALLENGE':'挑战方向','OPEN':'尚不取方向'}[c['stance']]
        return True,None,f'待验证假设：{info[0]}；{direction}。需要：{info[1]}。证伪条件：{info[2]}。引用：'+', '.join(refs)
    if c['kind']!='FACT' or len(refs)!=1 or c['topic']!='NONE' or c['stance']!='NONE':
        return False,'FACT_TYPE_INVALID','未支持：事实必须使用单一来源与明确计量类型。'
    obs=catalog[refs[0]];relation=c['relation']
    if c['measure']!=obs['measure']:
        return False,'MEASUREMENT_MISMATCH',f"未支持：{refs[0]} 的类型是 {obs['measure']}，不能改称 {c['measure']}。"
    if relation not in RELATIONS:
        return False,'RELATION_INVALID','未支持：关系不在核验范围。'
    value,prior=obs['current'],obs['comparison']
    if obs['measure'] in ('HOLDINGS','VALUATION'):
        ok=relation=='UNKNOWN';word='未知，不能当作零或已具备依据'
    elif obs['measure']=='RECOGNITION':
        ok=relation=='NO_NEW_CASH';word='本身不产生本期新现金，不能解释为本期现金来源'
    else:
        x=Decimal(value);y=Decimal(prior) if prior is not None else None
        ok={'VALUE':True,'POSITIVE':x>0,'NEGATIVE':x<0,'ZERO':x==0,
            'INCREASED':y is not None and x>y,'DECREASED':y is not None and x<y,
            'TURNED_NEGATIVE':y is not None and y>=0 and x<0}.get(relation,False)
        word=f"{value} {obs.get('currency','')}（日期 {obs.get('current_end','')}）"
        if prior is not None:word+=f"；比较值 {prior}（日期 {obs.get('comparison_end','')}）"
        word+='；'+{'VALUE':'来源列示值','POSITIVE':'本期为正','NEGATIVE':'本期为负','ZERO':'本期为零','INCREASED':'较比较值上升','DECREASED':'较比较值下降','TURNED_NEGATIVE':'由非负转为负'}.get(relation,'该推断不成立')
    if not ok:
        return False,'PROPOSITION_NOT_SUPPORTED',f"未支持：{refs[0]} 的 {relation} 推断；来源值为 {value}，比较值为 {prior}。"
    return True,None,f"{obs['label']}：{word}。[{refs[0]}]"


def _evaluate(task,catalog,priors):
    p=task.proposal
    if task.node not in NODES or set(p)!= {'claims','decisions'} or type(p['claims']) is not list or len(p['claims'])>8 or type(p['decisions']) is not list or len(p['decisions'])>64:
        raise ValueError('JUDGMENT_PROPOSAL_INVALID')
    if task.node!='catalog' and not p['claims']:raise ValueError('JUDGMENT_CLAIMS_REQUIRED')
    if task.node=='catalog' and (p['claims'] or p['decisions'] or priors):raise ValueError('JUDGMENT_CATALOG_INVALID')
    claims=[];ids=set()
    for c in p['claims']:
        if type(c) is not dict:raise ValueError('JUDGMENT_CLAIM_SHAPE_INVALID')
        ok,reason,text=_claim(c,catalog)
        if c['id'] in ids:raise ValueError('JUDGMENT_CLAIM_ID_DUPLICATE')
        ids.add(c['id']);claims.append({'id':c['id'],'kind':c['kind'],'supported':ok,'reason':reason,'text':text,'proposal':c})
    candidates={ref+':'+c['id']:c for ref,record in priors.items() for c in record['claims']}
    decisions={}
    if task.node not in MANAGERS and p['decisions']:raise ValueError('JUDGMENT_UNEXPECTED_DECISIONS')
    for d in p['decisions']:
        if type(d) is not dict or set(d)!= {'target','decision'} or d['target'] not in candidates or d['target'] in decisions or d['decision'] not in ('USE','REJECT','DEFER'):
            raise ValueError('JUDGMENT_DECISION_INVALID')
        decisions[d['target']]=d['decision']
    adjudications=[]
    if task.node in MANAGERS:
        for target,c in candidates.items():
            requested=decisions.get(target)
            effective=('REJECT' if not c['supported'] else requested or 'DEFER')
            reason=('UNSUPPORTED_CLAIM_CANNOT_BE_USED' if requested=='USE' and not c['supported'] else
                    'MISSING_MANAGER_DECISION' if requested is None else None)
            adjudications.append({'target':target,'requested':requested,'effective':effective,'reason':reason,
                'kind':c['kind'],'text':c['text']})
    return {'schema_version':SCHEMA,'task':{'financial_ref':task.financial_ref.run_id,'market_ref':task.market_ref.run_id,
        'node':task.node,'proposal':p,'prior_refs':[r.run_id for r in task.prior_refs]},'catalog':catalog,
        'hypotheses':{k:{'question':v[0],'needed':v[1],'falsifier':v[2],'references':v[3]} for k,v in HYPOTHESES.items()},
        'claims':claims,'adjudications':adjudications,
        'issues':[c['reason'] for c in claims if c['reason']]+[d['reason'] for d in adjudications if d['reason']],
        'investment_conclusion':'INSUFFICIENT_VALUATION_AND_PORTFOLIO_INPUT',
        'assurance':'BOUNDED_TYPED_PROPOSITIONS_ONLY_NOT_FREE_TEXT_OR_INVESTMENT_APPROVAL',
        'decision':'HUMAN_REVIEW'}


def read_record(root,ref):
    raw=(root/'runs'/ref.run_id/'judgment.json').read_bytes()
    if len(raw)>1024*1024 or sha256_hex(raw)!=ref.run_id:raise ValueError('JUDGMENT_HASH_MISMATCH')
    r=json.loads(raw)
    if r.get('schema_version')!=SCHEMA or canonical_json_bytes(r)!=raw:raise ValueError('JUDGMENT_RECORD_INVALID')
    return r


def _task(r):
    p=r['task']
    return ReviewNativeJudgment(RunRef(p['financial_ref']),RunRef(p['market_ref']),p['node'],p['proposal'],tuple(RunRef(x) for x in p['prior_refs']))


def _prior_tree(root,refs,task,catalog,cache):
    result={}
    for ref in refs:
        r=cache.get(ref.run_id)
        if r is None:r=read_record(root,ref)
        t=_task(r)
        # Cached node content does not validate a new incoming dependency edge.
        # The source identity and chronological relation belong to every edge.
        if (t.financial_ref!=task.financial_ref or t.market_ref!=task.market_ref or
                NODES.index(t.node)>=NODES.index(task.node)):
            raise ValueError('JUDGMENT_PRIOR_CONTEXT_INVALID')
        if ref.run_id not in cache:
            parents=_prior_tree(root,t.prior_refs,t,catalog,cache)
            if _evaluate(t,catalog,parents)!=r:raise ValueError('JUDGMENT_PRIOR_REPLAY_MISMATCH')
            cache[ref.run_id]=r
        result[ref.run_id]=cache[ref.run_id]
    return result


def run(task,root):
    snapshot=canonical_json_bytes(task.proposal)
    if len(snapshot)>128*1024:raise ValueError('JUDGMENT_PROPOSAL_TOO_LARGE')
    task=replace(task,proposal=json.loads(snapshot))
    catalog=_sources(root,task);priors=_prior_tree(root,task.prior_refs,task,catalog,{})
    record=_evaluate(task,catalog,priors);raw=canonical_json_bytes(record);ref=RunRef(sha256_hex(raw))
    if len(raw)>1024*1024:raise ValueError('JUDGMENT_RECORD_TOO_LARGE')
    write_once(root/'runs'/ref.run_id/'judgment.json',raw)
    return CashflowOutcome(ref,Decision.HUMAN_REVIEW,None,record)


def replay(root,ref):
    try:
        r=read_record(root,ref);task=_task(r);catalog=_sources(root,task)
        priors=_prior_tree(root,task.prior_refs,task,catalog,{})
        if _evaluate(task,catalog,priors)!=r:raise ValueError('JUDGMENT_REPLAY_MISMATCH')
        return ReplayReport('finauditgate.native-judgment-replay/v1',ref,True,Decision.HUMAN_REVIEW,None,None,3)
    except (ValueError,KeyError,TypeError,OSError,ArithmeticError) as exc:
        return ReplayReport('finauditgate.native-judgment-replay/v1',ref,False,None,None,None,0,str(exc))


def render_checked(record):
    parts=['已核对论点（金额为来源原单位，类型与期间不可互换）：']
    parts += [c['text'] for c in record['claims'] if c['supported']]
    if record['adjudications']:
        parts.append('经理逐项裁决：')
        parts += [f"{d['target']}：模型 {d['requested'] or '未回答'}；核心结果 {d['effective']}。"+
                  (d['text'] if d['effective']=='USE' else '该论点不进入结论。') for d in record['adjudications']]
    if record['issues']:parts.append('未通过或待处理项：'+', '.join(record['issues']))
    parts.append('尚无法判断投资吸引力；估值、预期收益及实际持仓输入不足。未知持仓不是零持仓。不给出入场价、止损价、目标价或仓位指令。')
    return '\n\n'.join(parts)
