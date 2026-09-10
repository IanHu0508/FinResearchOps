"""Sequence of core-owned reviews, bound to actual native model messages."""

from finauditgate.research import ReviewNativeJudgment
from finauditgate.contracts import RunRef
from finauditgate.core.judgment import read_record, NODES


def select_prior(rows,node):
    names=({'Bull Researcher','Bear Researcher'} if node=='Research Manager' else
           {'Research Manager','Trader','Aggressive Analyst','Conservative Analyst','Neutral Analyst'} if node=='Portfolio Manager' else set(NODES))
    return [r for r in rows if r['node'] in names]


def candidates(records):
    return [{'target':ref+':'+c['id'],'kind':c['kind'],'supported':c['supported'],
             'reason':c['reason'],'text':c['text']} for ref,record in records.items() for c in record['claims']]


def add_catalog(packet,outcome):
    return {**packet,'schema_version':'finresearchops.native-audit-packet/v5',
            'judgment_catalog_run_id':outcome.run_ref.run_id,
            'judgment_catalog':outcome.report['catalog'],'hypothesis_catalog':outcome.report['hypotheses']}


class JudgmentSession:
    def __init__(self,gate,financial_ref,market_ref):
        self.gate=gate;self.financial_ref=financial_ref;self.market_ref=market_ref
        self.rows=[];self.records={}
        self.catalog=gate.run(ReviewNativeJudgment(financial_ref,market_ref,'catalog',{'claims':[],'decisions':[]}))

    def __call__(self,node,proposal=None,model_message_id=None,draft=None):
        prior=select_prior(self.rows,node)
        if proposal is None:
            return candidates({r['run_id']:self.records[r['run_id']] for r in prior})
        if any(r['node']==node for r in self.rows):raise ValueError('JUDGMENT_NODE_REPEATED')
        outcome=self.gate.run(ReviewNativeJudgment(self.financial_ref,self.market_ref,node,proposal,
            tuple(RunRef(r['run_id']) for r in prior)))
        self.records[outcome.run_ref.run_id]=outcome.report
        self.rows.append({'node':node,'run_id':outcome.run_ref.run_id,'model_message_id':model_message_id,'draft':draft})
        return outcome.report


def validate(application,result,packet):
    from finauditgate.adapters.native_judgment import prompt,ANALYSTS,REPORTS,checked_text
    from finauditgate.core.judgment import render_checked
    rows=result['judgment_reviews']
    if [r['node'] for r in rows]!=list(NODES[1:]):raise ValueError('JUDGMENT_NODE_COVERAGE_INVALID')
    prior=[];records={}
    for row in rows:
        if set(row)!= {'node','run_id','model_message_id','draft'}:raise ValueError('JUDGMENT_BINDING_INVALID')
        ref=RunRef(row['run_id'])
        if not application._offline_gate.replay(ref).consistent:raise ValueError('JUDGMENT_CORE_REPLAY_FAILED')
        r=read_record(application._core_root,ref);t=r['task']
        selected=select_prior(prior,row['node'])
        if (t['node']!=row['node'] or t['financial_ref']!=packet['core_run_id'] or t['market_ref']!=packet['market_run_id']
                or t['prior_refs']!=[p['run_id'] for p in selected]):raise ValueError('JUDGMENT_SOURCE_BINDING_INVALID')
        matches=[(i,c,m) for i,c in enumerate(result['model_calls']) for m in c['output'] if m.get('id')==row['model_message_id']]
        if len(matches)!=1:raise ValueError('JUDGMENT_MODEL_BINDING_INVALID')
        index,call,message=matches[0]
        proposal=(message.get('tool_calls') or [{}])[0].get('args')
        if proposal is None:
            import json
            proposal=json.loads(message['content'])
        if call['node']!=row['node'] or proposal!=t['proposal']:raise ValueError('JUDGMENT_PROPOSAL_NOT_ACTUAL_OUTPUT')
        expected=prompt(row['node'],packet,candidates({p['run_id']:records[p['run_id']] for p in selected}),row['draft'])
        actual=[{'role':{'human':'user'}.get(m['type'],m['type']),'content':m['content']} for m in call['messages'][0]]
        if actual!=expected:raise ValueError('JUDGMENT_ACTUAL_INPUT_MISMATCH')
        if row['node'] in ANALYSTS:
            originals=[c for c in result['model_calls'][:index] if c['node']==row['node']]
            if not originals or originals[-1]['output'][0]['content']!=row['draft']:raise ValueError('JUDGMENT_DRAFT_BINDING_INVALID')
        elif row['draft'] is not None:raise ValueError('JUDGMENT_UNEXPECTED_DRAFT')
        if row['node'] in REPORTS and result['reports'][REPORTS[row['node']]]!=checked_text(row['node'],r):
            raise ValueError('JUDGMENT_REPORT_NOT_CHECKED_PROJECTION')
        # Every downstream report/history must receive the deterministic checked
        # text. The raw proposition JSON and drafts are kept separately.
        outputs=[c['output'] for c in result['node_calls'] if c['node']==row['node']]
        if not outputs or render_checked(r) not in str(outputs[-1]):
            # str(dict) escapes newlines; check the recursively nested values.
            def strings(x):
                if isinstance(x,str):yield x
                elif isinstance(x,dict):
                    for value in x.values():yield from strings(value)
                elif isinstance(x,list):
                    for value in x:yield from strings(value)
            if not outputs or not any(render_checked(r) in x for x in strings(outputs[-1])):
                raise ValueError('JUDGMENT_CHECKED_OUTPUT_MISSING')
        prior.append(row);records[row['run_id']]=r
    return records
