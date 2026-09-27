"""JSON Schemas of the v18 thesis stages, exported from the Pydantic models; do not edit by hand.

The standard-library reader proves a format failure against these frozen schemas.
Integration tests require them to equal the live model schemas.
"""

SCHEMAS = {'AnalystReport': {'$defs': {'Observation': {'additionalProperties': False,
                                             'properties': {'statement': {'minLength': 1,
                                                                          'title': 'Statement',
                                                                          'type': 'string'},
                                                            'evidence_refs': {'items': {'type': 'string'},
                                                                              'maxItems': 48,
                                                                              'minItems': 1,
                                                                              'title': 'Evidence Refs',
                                                                              'type': 'array'}},
                                             'required': ['statement', 'evidence_refs'],
                                             'title': 'Observation',
                                             'type': 'object'}},
                   'additionalProperties': False,
                   'properties': {'analysis': {'minLength': 1, 'title': 'Analysis', 'type': 'string'},
                                  'observations': {'items': {'$ref': '#/$defs/Observation'},
                                                   'maxItems': 12,
                                                   'title': 'Observations',
                                                   'type': 'array'},
                                  'coverage': {'enum': ['available', 'partial', 'unavailable'],
                                               'title': 'Coverage',
                                               'type': 'string'},
                                  'limits': {'items': {'type': 'string'},
                                             'maxItems': 12,
                                             'title': 'Limits',
                                             'type': 'array'},
                                  'evidence_refs': {'items': {'type': 'string'},
                                                    'maxItems': 48,
                                                    'title': 'Evidence Refs',
                                                    'type': 'array'}},
                   'required': ['analysis', 'observations', 'coverage', 'limits', 'evidence_refs'],
                   'title': 'AnalystReport',
                   'type': 'object'},
 'ExecutionReview': {'$defs': {'TraderProposal': {'additionalProperties': False,
                                                  'properties': {'action': {'enum': ['Buy', 'Hold', 'Sell'],
                                                                            'title': 'Action',
                                                                            'type': 'string'},
                                                                 'reasoning': {'minLength': 1,
                                                                               'title': 'Reasoning',
                                                                               'type': 'string'},
                                                                 'entry_price': {'anyOf': [{'type': 'number'},
                                                                                           {'type': 'null'}],
                                                                                 'default': None,
                                                                                 'title': 'Entry Price'},
                                                                 'stop_loss': {'anyOf': [{'type': 'number'},
                                                                                         {'type': 'null'}],
                                                                               'default': None,
                                                                               'title': 'Stop Loss'},
                                                                 'position_sizing': {'anyOf': [{'type': 'string'},
                                                                                               {'type': 'null'}],
                                                                                     'default': None,
                                                                                     'title': 'Position Sizing'}},
                                                  'required': ['action', 'reasoning'],
                                                  'title': 'TraderProposal',
                                                  'type': 'object'}},
                     'additionalProperties': False,
                     'description': 'Complete JSON execution review, not a free-text transaction report.',
                     'properties': {'proposal': {'$ref': '#/$defs/TraderProposal'},
                                    'feasibility_conditions': {'items': {'type': 'string'},
                                                               'maxItems': 6,
                                                               'minItems': 1,
                                                               'title': 'Feasibility Conditions',
                                                               'type': 'array'},
                                    'missing_portfolio_inputs': {'items': {'type': 'string'},
                                                                 'maxItems': 6,
                                                                 'title': 'Missing Portfolio Inputs',
                                                                 'type': 'array'}},
                     'required': ['proposal', 'feasibility_conditions', 'missing_portfolio_inputs'],
                     'title': 'ExecutionReview',
                     'type': 'object'},
 'FinalResearchReport': {'$defs': {'BeliefExplanation': {'additionalProperties': False,
                                                         'properties': {'belief_id': {'title': 'Belief Id',
                                                                                      'type': 'string'},
                                                                        'explanation': {'$ref': '#/$defs/ResearchBlock'}},
                                                         'required': ['belief_id', 'explanation'],
                                                         'title': 'BeliefExplanation',
                                                         'type': 'object'},
                                   'ChangeExplanation': {'additionalProperties': False,
                                                         'properties': {'scenario_id': {'anyOf': [{'enum': ['F1',
                                                                                                            'F2',
                                                                                                            'F3'],
                                                                                                   'type': 'string'},
                                                                                                  {'type': 'null'}],
                                                                                        'title': 'Scenario Id'},
                                                                        'field': {'enum': ['market_price',
                                                                                           'shares_per_traded_unit',
                                                                                           'revenue',
                                                                                           'operating_margin',
                                                                                           'net_nonoperating_income',
                                                                                           'effective_tax_rate',
                                                                                           'noncontrolling_attribution',
                                                                                           'diluted_ordinary_shares',
                                                                                           'non_working_capital_adjustments',
                                                                                           'operating_asset_liability_cash_effect',
                                                                                           'cash_capex',
                                                                                           'fx_reporting_per_price_currency',
                                                                                           'exit_pe',
                                                                                           'cash_dividend_per_traded_unit'],
                                                                                  'title': 'Field',
                                                                                  'type': 'string'},
                                                                        'explanation': {'$ref': '#/$defs/ResearchBlock'}},
                                                         'required': ['scenario_id', 'field', 'explanation'],
                                                         'title': 'ChangeExplanation',
                                                         'type': 'object'},
                                   'ResearchBlock': {'additionalProperties': False,
                                                     'properties': {'text': {'description': 'Concise economic '
                                                                                            'explanation. Refer to '
                                                                                            'forward quantities '
                                                                                            'through metrics; do not '
                                                                                            'retype forecast '
                                                                                            'amounts/ratios, create a '
                                                                                            'new target, or repeat the '
                                                                                            'table. Historical claims '
                                                                                            'still need source '
                                                                                            'support.',
                                                                             'maxLength': 2400,
                                                                             'minLength': 1,
                                                                             'title': 'Text',
                                                                             'type': 'string'},
                                                                    'evidence_refs': {'description': 'Usually omit: '
                                                                                                     'program derives '
                                                                                                     'sources from '
                                                                                                     'inline '
                                                                                                     'evidence-block '
                                                                                                     'references.',
                                                                                      'items': {'type': 'string'},
                                                                                      'maxItems': 48,
                                                                                      'title': 'Evidence Refs',
                                                                                      'type': 'array'},
                                                                    'metrics': {'description': 'Usually omit: program '
                                                                                               'derives metrics from '
                                                                                               'inline references. No '
                                                                                               'numeric values or '
                                                                                               'labels.',
                                                                                'items': {'$ref': '#/$defs/ResearchMetric'},
                                                                                'maxItems': 48,
                                                                                'title': 'Metrics',
                                                                                'type': 'array'}},
                                                     'required': ['text'],
                                                     'title': 'ResearchBlock',
                                                     'type': 'object'},
                                   'ResearchMetric': {'additionalProperties': False,
                                                      'properties': {'scenario_id': {'enum': ['F1', 'F2', 'F3'],
                                                                                     'title': 'Scenario Id',
                                                                                     'type': 'string'},
                                                                     'metric': {'enum': ['revenue',
                                                                                         'operating_margin',
                                                                                         'effective_tax_rate',
                                                                                         'operating_profit',
                                                                                         'pretax_income',
                                                                                         'consolidated_net_income',
                                                                                         'noncontrolling_attribution_effect',
                                                                                         'parent_net_income',
                                                                                         'eps_per_traded_unit',
                                                                                         'operating_cash_flow',
                                                                                         'cash_after_capex_proxy',
                                                                                         'exit_price_per_traded_unit',
                                                                                         'price_only_break_even_pe',
                                                                                         'dividend_adjusted_break_even_pe',
                                                                                         'return_ex_dividend',
                                                                                         'return_with_dividend'],
                                                                                'title': 'Metric',
                                                                                'type': 'string'}},
                                                      'required': ['scenario_id', 'metric'],
                                                      'title': 'ResearchMetric',
                                                      'type': 'object'},
                                   'ResearchSections': {'additionalProperties': False,
                                                        'properties': {'operating_performance': {'$ref': '#/$defs/ResearchBlock'},
                                                                       'earnings_quality': {'$ref': '#/$defs/ResearchBlock'},
                                                                       'cash_and_capital_allocation': {'$ref': '#/$defs/ResearchBlock'},
                                                                       'valuation_and_price_requirements': {'$ref': '#/$defs/ResearchBlock'}},
                                                        'required': ['operating_performance',
                                                                     'earnings_quality',
                                                                     'cash_and_capital_allocation',
                                                                     'valuation_and_price_requirements'],
                                                        'title': 'ResearchSections',
                                                        'type': 'object'},
                                   'ScenarioUse': {'additionalProperties': False,
                                                   'properties': {'scenario_id': {'enum': ['F1', 'F2', 'F3'],
                                                                                  'title': 'Scenario Id',
                                                                                  'type': 'string'},
                                                                  'disposition': {'enum': ['use',
                                                                                           'conditional',
                                                                                           'reject'],
                                                                                  'title': 'Disposition',
                                                                                  'type': 'string'},
                                                                  'reason': {'maxLength': 1600,
                                                                             'minLength': 1,
                                                                             'title': 'Reason',
                                                                             'type': 'string'},
                                                                  'what_changes_the_view': {'maxLength': 1200,
                                                                                            'minLength': 1,
                                                                                            'title': 'What Changes The '
                                                                                                     'View',
                                                                                            'type': 'string'},
                                                                  'metrics': {'description': 'Optional extra metric '
                                                                                             'selections; inline '
                                                                                             'selections are derived '
                                                                                             'automatically.',
                                                                              'items': {'$ref': '#/$defs/ResearchMetric'},
                                                                              'maxItems': 48,
                                                                              'title': 'Metrics',
                                                                              'type': 'array'}},
                                                   'required': ['scenario_id',
                                                                'disposition',
                                                                'reason',
                                                                'what_changes_the_view'],
                                                   'title': 'ScenarioUse',
                                                   'type': 'object'}},
                         'additionalProperties': False,
                         'properties': {'rating': {'enum': ['Buy',
                                                            'Overweight',
                                                            'Hold',
                                                            'Underweight',
                                                            'Sell',
                                                            'REVIEW'],
                                                   'title': 'Rating',
                                                   'type': 'string'},
                                        'summary': {'$ref': '#/$defs/ResearchBlock'},
                                        'financial_analysis': {'$ref': '#/$defs/ResearchSections'},
                                        'strongest_counterevidence': {'$ref': '#/$defs/ResearchBlock'},
                                        'scenario_assessments': {'items': {'$ref': '#/$defs/ScenarioUse'},
                                                                 'maxItems': 3,
                                                                 'title': 'Scenario Assessments',
                                                                 'type': 'array'},
                                        'limitations': {'items': {'type': 'string'},
                                                        'maxItems': 6,
                                                        'title': 'Limitations',
                                                        'type': 'array'},
                                        'change_explanations': {'items': {'$ref': '#/$defs/ChangeExplanation'},
                                                                'maxItems': 12,
                                                                'title': 'Change Explanations',
                                                                'type': 'array'},
                                        'belief_explanations': {'items': {'$ref': '#/$defs/BeliefExplanation'},
                                                                'maxItems': 4,
                                                                'minItems': 2,
                                                                'title': 'Belief Explanations',
                                                                'type': 'array'}},
                         'required': ['rating',
                                      'summary',
                                      'financial_analysis',
                                      'strongest_counterevidence',
                                      'scenario_assessments',
                                      'limitations',
                                      'change_explanations',
                                      'belief_explanations'],
                         'title': 'FinalResearchReport',
                         'type': 'object'},
 'ForwardRevision': {'$defs': {'AttributionAmount': {'additionalProperties': False,
                                                     'properties': {'value': {'anyOf': [{'minimum': 0,
                                                                                         'type': 'number'},
                                                                                        {'type': 'null'}],
                                                                              'description': 'Absolute nonnegative '
                                                                                             'amount in millions, or '
                                                                                             'null. Never copy a '
                                                                                             "statement's deduction "
                                                                                             'minus sign into this '
                                                                                             'amount.',
                                                                              'title': 'Value'},
                                                                    'basis_type': {'enum': ['reported',
                                                                                            'company_guidance',
                                                                                            'reference_comparison',
                                                                                            'analyst_assumption'],
                                                                                   'title': 'Basis Type',
                                                                                   'type': 'string'},
                                                                    'reason': {'description': 'Explain the input, '
                                                                                              'historical anchor and '
                                                                                              'forward change; a cited '
                                                                                              'historical number does '
                                                                                              'not certify a forecast.',
                                                                               'minLength': 1,
                                                                               'title': 'Reason',
                                                                               'type': 'string'},
                                                                    'evidence_refs': {'items': {'type': 'string'},
                                                                                      'maxItems': 6,
                                                                                      'title': 'Evidence Refs',
                                                                                      'type': 'array'}},
                                                     'required': ['value', 'basis_type', 'reason', 'evidence_refs'],
                                                     'title': 'AttributionAmount',
                                                     'type': 'object'},
                               'ExpectedAttribution': {'additionalProperties': False,
                                                       'properties': {'nature': {'enum': ['profit', 'loss', 'unknown'],
                                                                                 'title': 'Nature',
                                                                                 'type': 'string'},
                                                                      'amount': {'anyOf': [{'type': 'number'},
                                                                                           {'type': 'null'}],
                                                                                 'title': 'Amount'}},
                                                       'required': ['nature', 'amount'],
                                                       'title': 'ExpectedAttribution',
                                                       'type': 'object'},
                               'ForecastAssumption': {'additionalProperties': False,
                                                      'properties': {'value': {'anyOf': [{'type': 'number'},
                                                                                         {'type': 'null'}],
                                                                               'description': 'Numeric input, or null '
                                                                                              'when unavailable. '
                                                                                              'Ratios are fractions, '
                                                                                              'not percentage points. '
                                                                                              'Do not use zero for '
                                                                                              'unknown.',
                                                                               'title': 'Value'},
                                                                     'basis_type': {'enum': ['reported',
                                                                                             'company_guidance',
                                                                                             'reference_comparison',
                                                                                             'analyst_assumption'],
                                                                                    'title': 'Basis Type',
                                                                                    'type': 'string'},
                                                                     'reason': {'description': 'Explain the input, '
                                                                                               'historical anchor and '
                                                                                               'forward change; a '
                                                                                               'cited historical '
                                                                                               'number does not '
                                                                                               'certify a forecast.',
                                                                                'minLength': 1,
                                                                                'title': 'Reason',
                                                                                'type': 'string'},
                                                                     'evidence_refs': {'items': {'type': 'string'},
                                                                                       'maxItems': 6,
                                                                                       'title': 'Evidence Refs',
                                                                                       'type': 'array'}},
                                                      'required': ['value', 'basis_type', 'reason', 'evidence_refs'],
                                                      'title': 'ForecastAssumption',
                                                      'type': 'object'},
                               'ForwardChange': {'additionalProperties': False,
                                                 'properties': {'scenario_id': {'anyOf': [{'enum': ['F1', 'F2', 'F3'],
                                                                                           'type': 'string'},
                                                                                          {'type': 'null'}],
                                                                                'description': 'Null only for common '
                                                                                               'market_price or '
                                                                                               'shares_per_traded_unit.',
                                                                                'title': 'Scenario Id'},
                                                                'field': {'enum': ['market_price',
                                                                                   'shares_per_traded_unit',
                                                                                   'revenue',
                                                                                   'operating_margin',
                                                                                   'net_nonoperating_income',
                                                                                   'effective_tax_rate',
                                                                                   'noncontrolling_attribution',
                                                                                   'diluted_ordinary_shares',
                                                                                   'non_working_capital_adjustments',
                                                                                   'operating_asset_liability_cash_effect',
                                                                                   'cash_capex',
                                                                                   'fx_reporting_per_price_currency',
                                                                                   'exit_pe',
                                                                                   'cash_dividend_per_traded_unit'],
                                                                          'title': 'Field',
                                                                          'type': 'string'},
                                                                'expected_before': {'anyOf': [{'type': 'number'},
                                                                                              {'$ref': '#/$defs/ExpectedAttribution'},
                                                                                              {'type': 'null'}],
                                                                                    'description': 'Copy the proposed '
                                                                                                   'input VALUE '
                                                                                                   'exactly, not a '
                                                                                                   'historical source '
                                                                                                   'value. For NCI use '
                                                                                                   'its proposed '
                                                                                                   'nature and '
                                                                                                   'amount.value.',
                                                                                    'title': 'Expected Before'},
                                                                'replacement': {'anyOf': [{'$ref': '#/$defs/ForecastAssumption'},
                                                                                          {'$ref': '#/$defs/NoncontrollingAttribution'}],
                                                                                'description': 'Complete corrected '
                                                                                               'assumption including '
                                                                                               'reason and '
                                                                                               'evidence_refs; NCI is '
                                                                                               'a complete '
                                                                                               'nature+amount object. '
                                                                                               'Unknown uses '
                                                                                               'value:null, never '
                                                                                               'guessed zero.',
                                                                                'title': 'Replacement'},
                                                                'correction_basis': {'enum': ['source_misread',
                                                                                              'accounting_correction',
                                                                                              'assumption_update'],
                                                                                     'title': 'Correction Basis',
                                                                                     'type': 'string'},
                                                                'reason': {'maxLength': 1600,
                                                                           'minLength': 1,
                                                                           'title': 'Reason',
                                                                           'type': 'string'},
                                                                'evidence_refs': {'items': {'type': 'string'},
                                                                                  'maxItems': 8,
                                                                                  'minItems': 1,
                                                                                  'title': 'Evidence Refs',
                                                                                  'type': 'array'}},
                                                 'required': ['scenario_id',
                                                              'field',
                                                              'expected_before',
                                                              'replacement',
                                                              'correction_basis',
                                                              'reason',
                                                              'evidence_refs'],
                                                 'title': 'ForwardChange',
                                                 'type': 'object'},
                               'NoncontrollingAttribution': {'additionalProperties': False,
                                                             'properties': {'nature': {'description': 'Economic '
                                                                                                      'nature: profit '
                                                                                                      'attributable to '
                                                                                                      'outside owners '
                                                                                                      'is deducted; '
                                                                                                      'their loss is '
                                                                                                      'added back. A '
                                                                                                      'statement may '
                                                                                                      'print a PROFIT '
                                                                                                      'deduction in '
                                                                                                      'parentheses; '
                                                                                                      'parentheses do '
                                                                                                      'not make it a '
                                                                                                      'loss.',
                                                                                       'enum': ['profit',
                                                                                                'loss',
                                                                                                'unknown'],
                                                                                       'title': 'Nature',
                                                                                       'type': 'string'},
                                                                            'amount': {'$ref': '#/$defs/AttributionAmount'}},
                                                             'required': ['nature', 'amount'],
                                                             'title': 'NoncontrollingAttribution',
                                                             'type': 'object'},
                               'ShortBeliefUpdate': {'additionalProperties': False,
                                                     'properties': {'belief_id': {'title': 'Belief Id',
                                                                                  'type': 'string'},
                                                                    'status': {'enum': ['maintain',
                                                                                        'revise',
                                                                                        'withdraw',
                                                                                        'unresolved'],
                                                                               'title': 'Status',
                                                                               'type': 'string'},
                                                                    'new_statement': {'anyOf': [{'type': 'string'},
                                                                                                {'type': 'null'}],
                                                                                      'title': 'New Statement'},
                                                                    'update_basis': {'enum': ['source_observation',
                                                                                              'reasoning_correction',
                                                                                              'assumption_change',
                                                                                              'no_new_basis'],
                                                                                     'title': 'Update Basis',
                                                                                     'type': 'string'},
                                                                    'reason': {'maxLength': 1600,
                                                                               'minLength': 1,
                                                                               'title': 'Reason',
                                                                               'type': 'string'},
                                                                    'financial_implication': {'maxLength': 1600,
                                                                                              'minLength': 1,
                                                                                              'title': 'Financial '
                                                                                                       'Implication',
                                                                                              'type': 'string'},
                                                                    'evidence_refs': {'items': {'type': 'string'},
                                                                                      'maxItems': 8,
                                                                                      'title': 'Evidence Refs',
                                                                                      'type': 'array'}},
                                                     'required': ['belief_id',
                                                                  'status',
                                                                  'new_statement',
                                                                  'update_basis',
                                                                  'reason',
                                                                  'financial_implication',
                                                                  'evidence_refs'],
                                                     'title': 'ShortBeliefUpdate',
                                                     'type': 'object'},
                               'ShortClaimAssessment': {'additionalProperties': False,
                                                        'properties': {'claim_id': {'title': 'Claim Id',
                                                                                    'type': 'string'},
                                                                       'disposition': {'enum': ['use',
                                                                                                'conditional',
                                                                                                'reject'],
                                                                                       'title': 'Disposition',
                                                                                       'type': 'string'},
                                                                       'reason': {'maxLength': 1600,
                                                                                  'minLength': 1,
                                                                                  'title': 'Reason',
                                                                                  'type': 'string'}},
                                                        'required': ['claim_id', 'disposition', 'reason'],
                                                        'title': 'ShortClaimAssessment',
                                                        'type': 'object'}},
                     'additionalProperties': False,
                     'properties': {'changes': {'description': 'Only justified changes. Empty is valid; do not redraw '
                                                               'all forecasts or fit inputs to a desired price/rating.',
                                                'items': {'$ref': '#/$defs/ForwardChange'},
                                                'maxItems': 12,
                                                'title': 'Changes',
                                                'type': 'array'},
                                    'claim_assessments': {'items': {'$ref': '#/$defs/ShortClaimAssessment'},
                                                          'maxItems': 8,
                                                          'title': 'Claim Assessments',
                                                          'type': 'array'},
                                    'belief_updates': {'items': {'$ref': '#/$defs/ShortBeliefUpdate'},
                                                       'maxItems': 4,
                                                       'minItems': 2,
                                                       'title': 'Belief Updates',
                                                       'type': 'array'},
                                    'unresolved_issues': {'items': {'type': 'string'},
                                                          'maxItems': 6,
                                                          'title': 'Unresolved Issues',
                                                          'type': 'array'}},
                     'required': ['changes', 'claim_assessments', 'belief_updates', 'unresolved_issues'],
                     'title': 'ForwardRevision',
                     'type': 'object'},
 'IndependentAssessment': {'$defs': {'IndependentBelief': {'additionalProperties': False,
                                                           'properties': {'statement': {'minLength': 1,
                                                                                        'title': 'Statement',
                                                                                        'type': 'string'},
                                                                          'business_mechanism': {'minLength': 1,
                                                                                                 'title': 'Business '
                                                                                                          'Mechanism',
                                                                                                 'type': 'string'},
                                                                          'evidence_refs': {'items': {'type': 'string'},
                                                                                            'maxItems': 8,
                                                                                            'title': 'Evidence Refs',
                                                                                            'type': 'array'},
                                                                          'would_change_mind': {'minLength': 1,
                                                                                                'title': 'Would Change '
                                                                                                         'Mind',
                                                                                                'type': 'string'},
                                                                          'uncertainty': {'minLength': 1,
                                                                                          'title': 'Uncertainty',
                                                                                          'type': 'string'},
                                                                          'belief_id': {'description': 'D1, D2, ... in '
                                                                                                       'order; these '
                                                                                                       'are this '
                                                                                                       'independent '
                                                                                                       "draft's "
                                                                                                       'beliefs.',
                                                                                        'pattern': '^D[1-4]$',
                                                                                        'title': 'Belief Id',
                                                                                        'type': 'string'}},
                                                           'required': ['statement',
                                                                        'business_mechanism',
                                                                        'evidence_refs',
                                                                        'would_change_mind',
                                                                        'uncertainty',
                                                                        'belief_id'],
                                                           'title': 'IndependentBelief',
                                                           'type': 'object'},
                                     'InitialDecision': {'additionalProperties': False,
                                                         'properties': {'rating': {'description': 'Source-only '
                                                                                                  'research stance; '
                                                                                                  'use REVIEW when the '
                                                                                                  'price judgment '
                                                                                                  'lacks a defensible '
                                                                                                  'basis, not Hold as '
                                                                                                  'a placeholder.',
                                                                                   'enum': ['Buy',
                                                                                            'Overweight',
                                                                                            'Hold',
                                                                                            'Underweight',
                                                                                            'Sell',
                                                                                            'REVIEW'],
                                                                                   'title': 'Rating',
                                                                                   'type': 'string'},
                                                                        'executive_summary': {'description': 'One '
                                                                                                             'concise '
                                                                                                             'reason '
                                                                                                             'grounded '
                                                                                                             'in the '
                                                                                                             'beliefs '
                                                                                                             'above. '
                                                                                                             'No '
                                                                                                             'second '
                                                                                                             'investment '
                                                                                                             'thesis '
                                                                                                             'or '
                                                                                                             'target-price '
                                                                                                             'essay.',
                                                                                              'minLength': 1,
                                                                                              'title': 'Executive '
                                                                                                       'Summary',
                                                                                              'type': 'string'}},
                                                         'required': ['rating', 'executive_summary'],
                                                         'title': 'InitialDecision',
                                                         'type': 'object'}},
                           'additionalProperties': False,
                           'description': 'Source-only beliefs first, then a compact initial stance; no duplicate '
                                          'financial report.',
                           'properties': {'beliefs': {'items': {'$ref': '#/$defs/IndependentBelief'},
                                                      'maxItems': 4,
                                                      'minItems': 2,
                                                      'title': 'Beliefs',
                                                      'type': 'array'},
                                          'decision': {'$ref': '#/$defs/InitialDecision'}},
                           'required': ['beliefs', 'decision'],
                           'title': 'IndependentAssessment',
                           'type': 'object'},
 'InitialBrief': {'$defs': {'Claim': {'additionalProperties': False,
                                      'properties': {'statement': {'minLength': 1,
                                                                   'title': 'Statement',
                                                                   'type': 'string'},
                                                     'business_mechanism': {'minLength': 1,
                                                                            'title': 'Business Mechanism',
                                                                            'type': 'string'},
                                                     'evidence_refs': {'items': {'type': 'string'},
                                                                       'maxItems': 8,
                                                                       'title': 'Evidence Refs',
                                                                       'type': 'array'},
                                                     'would_change_mind': {'minLength': 1,
                                                                           'title': 'Would Change Mind',
                                                                           'type': 'string'},
                                                     'uncertainty': {'minLength': 1,
                                                                     'title': 'Uncertainty',
                                                                     'type': 'string'}},
                                      'required': ['statement',
                                                   'business_mechanism',
                                                   'evidence_refs',
                                                   'would_change_mind',
                                                   'uncertainty'],
                                      'title': 'Claim',
                                      'type': 'object'}},
                  'additionalProperties': False,
                  'description': 'Complete JSON response object with every required field and no surrounding prose.',
                  'properties': {'claims': {'items': {'$ref': '#/$defs/Claim'},
                                            'maxItems': 4,
                                            'minItems': 2,
                                            'title': 'Claims',
                                            'type': 'array'}},
                  'required': ['claims'],
                  'title': 'InitialBrief',
                  'type': 'object'},
 'ResearchEvaluation': {'$defs': {'Assessment': {'additionalProperties': False,
                                                 'properties': {'claim_id': {'title': 'Claim Id', 'type': 'string'},
                                                                'disposition': {'enum': ['use',
                                                                                         'conditional',
                                                                                         'reject'],
                                                                                'title': 'Disposition',
                                                                                'type': 'string'},
                                                                'reason': {'minLength': 1,
                                                                           'title': 'Reason',
                                                                           'type': 'string'}},
                                                 'required': ['claim_id', 'disposition', 'reason'],
                                                 'title': 'Assessment',
                                                 'type': 'object'},
                                  'ResearchPlan': {'additionalProperties': False,
                                                   'properties': {'recommendation': {'description': 'Research stance '
                                                                                                    'from this '
                                                                                                    'analysis. REVIEW '
                                                                                                    'means '
                                                                                                    'insufficient '
                                                                                                    'basis to rate; '
                                                                                                    'Hold means a '
                                                                                                    'supported neutral '
                                                                                                    'view.',
                                                                                     'enum': ['Buy',
                                                                                              'Overweight',
                                                                                              'Hold',
                                                                                              'Underweight',
                                                                                              'Sell',
                                                                                              'REVIEW'],
                                                                                     'title': 'Recommendation',
                                                                                     'type': 'string'},
                                                                  'rationale': {'minLength': 1,
                                                                                'title': 'Rationale',
                                                                                'type': 'string'},
                                                                  'strategic_actions': {'description': 'Conditional '
                                                                                                       'next steps; do '
                                                                                                       'not invent '
                                                                                                       'holdings or '
                                                                                                       'personal risk '
                                                                                                       'constraints.',
                                                                                        'title': 'Strategic Actions',
                                                                                        'type': 'string'}},
                                                   'required': ['recommendation', 'rationale', 'strategic_actions'],
                                                   'title': 'ResearchPlan',
                                                   'type': 'object'}},
                        'additionalProperties': False,
                        'description': 'Complete JSON response containing plan, assessments and '
                                       'valuation_basis_and_gaps.',
                        'properties': {'plan': {'$ref': '#/$defs/ResearchPlan'},
                                       'assessments': {'items': {'$ref': '#/$defs/Assessment'},
                                                       'maxItems': 8,
                                                       'minItems': 4,
                                                       'title': 'Assessments',
                                                       'type': 'array'},
                                       'valuation_basis_and_gaps': {'minLength': 1,
                                                                    'title': 'Valuation Basis And Gaps',
                                                                    'type': 'string'}},
                        'required': ['plan', 'assessments', 'valuation_basis_and_gaps'],
                        'title': 'ResearchEvaluation',
                        'type': 'object'},
 'RevisionBrief': {'$defs': {'ClaimUpdate': {'additionalProperties': False,
                                             'properties': {'claim_id': {'title': 'Claim Id', 'type': 'string'},
                                                            'status': {'enum': ['maintain',
                                                                                'revise',
                                                                                'withdraw',
                                                                                'unresolved'],
                                                                       'title': 'Status',
                                                                       'type': 'string'},
                                                            'updated_statement': {'anyOf': [{'type': 'string'},
                                                                                            {'type': 'null'}],
                                                                                  'description': 'Only revise supplies '
                                                                                                 'a replacement; '
                                                                                                 'otherwise null.',
                                                                                  'title': 'Updated Statement'},
                                                            'reason': {'minLength': 1,
                                                                       'title': 'Reason',
                                                                       'type': 'string'},
                                                            'evidence_refs': {'items': {'type': 'string'},
                                                                              'maxItems': 8,
                                                                              'title': 'Evidence Refs',
                                                                              'type': 'array'},
                                                            'would_change_mind': {'minLength': 1,
                                                                                  'title': 'Would Change Mind',
                                                                                  'type': 'string'}},
                                             'required': ['claim_id',
                                                          'status',
                                                          'updated_statement',
                                                          'reason',
                                                          'evidence_refs',
                                                          'would_change_mind'],
                                             'title': 'ClaimUpdate',
                                             'type': 'object'},
                             'CounterResponse': {'additionalProperties': False,
                                                 'properties': {'claim_id': {'title': 'Claim Id', 'type': 'string'},
                                                                'response': {'enum': ['acknowledge',
                                                                                      'dispute',
                                                                                      'unresolved'],
                                                                             'title': 'Response',
                                                                             'type': 'string'},
                                                                'reason': {'minLength': 1,
                                                                           'title': 'Reason',
                                                                           'type': 'string'},
                                                                'evidence_refs': {'items': {'type': 'string'},
                                                                                  'maxItems': 8,
                                                                                  'title': 'Evidence Refs',
                                                                                  'type': 'array'}},
                                                 'required': ['claim_id', 'response', 'reason', 'evidence_refs'],
                                                 'title': 'CounterResponse',
                                                 'type': 'object'}},
                   'additionalProperties': False,
                   'description': 'Complete JSON response containing updates AND counter_responses together.',
                   'properties': {'updates': {'items': {'$ref': '#/$defs/ClaimUpdate'},
                                              'maxItems': 4,
                                              'minItems': 2,
                                              'title': 'Updates',
                                              'type': 'array'},
                                  'counter_responses': {'items': {'$ref': '#/$defs/CounterResponse'},
                                                        'maxItems': 4,
                                                        'minItems': 2,
                                                        'title': 'Counter Responses',
                                                        'type': 'array'}},
                   'required': ['updates', 'counter_responses'],
                   'title': 'RevisionBrief',
                   'type': 'object'},
 'RiskBrief': {'additionalProperties': False,
               'description': 'Complete JSON risk review containing every required field.',
               'properties': {'analysis': {'minLength': 1, 'title': 'Analysis', 'type': 'string'},
                              'evidence_refs': {'items': {'type': 'string'},
                                                'maxItems': 8,
                                                'title': 'Evidence Refs',
                                                'type': 'array'},
                              'invalidation_conditions': {'items': {'type': 'string'},
                                                          'maxItems': 5,
                                                          'minItems': 1,
                                                          'title': 'Invalidation Conditions',
                                                          'type': 'array'}},
               'required': ['analysis', 'evidence_refs', 'invalidation_conditions'],
               'title': 'RiskBrief',
               'type': 'object'},
 'UnderwritingDraft': {'$defs': {'AttributionAmount': {'additionalProperties': False,
                                                       'properties': {'value': {'anyOf': [{'minimum': 0,
                                                                                           'type': 'number'},
                                                                                          {'type': 'null'}],
                                                                                'description': 'Absolute nonnegative '
                                                                                               'amount in millions, or '
                                                                                               'null. Never copy a '
                                                                                               "statement's deduction "
                                                                                               'minus sign into this '
                                                                                               'amount.',
                                                                                'title': 'Value'},
                                                                      'basis_type': {'enum': ['reported',
                                                                                              'company_guidance',
                                                                                              'reference_comparison',
                                                                                              'analyst_assumption'],
                                                                                     'title': 'Basis Type',
                                                                                     'type': 'string'},
                                                                      'reason': {'description': 'Explain the input, '
                                                                                                'historical anchor and '
                                                                                                'forward change; a '
                                                                                                'cited historical '
                                                                                                'number does not '
                                                                                                'certify a forecast.',
                                                                                 'minLength': 1,
                                                                                 'title': 'Reason',
                                                                                 'type': 'string'},
                                                                      'evidence_refs': {'items': {'type': 'string'},
                                                                                        'maxItems': 6,
                                                                                        'title': 'Evidence Refs',
                                                                                        'type': 'array'}},
                                                       'required': ['value', 'basis_type', 'reason', 'evidence_refs'],
                                                       'title': 'AttributionAmount',
                                                       'type': 'object'},
                                 'ForecastAssumption': {'additionalProperties': False,
                                                        'properties': {'value': {'anyOf': [{'type': 'number'},
                                                                                           {'type': 'null'}],
                                                                                 'description': 'Numeric input, or '
                                                                                                'null when '
                                                                                                'unavailable. Ratios '
                                                                                                'are fractions, not '
                                                                                                'percentage points. Do '
                                                                                                'not use zero for '
                                                                                                'unknown.',
                                                                                 'title': 'Value'},
                                                                       'basis_type': {'enum': ['reported',
                                                                                               'company_guidance',
                                                                                               'reference_comparison',
                                                                                               'analyst_assumption'],
                                                                                      'title': 'Basis Type',
                                                                                      'type': 'string'},
                                                                       'reason': {'description': 'Explain the input, '
                                                                                                 'historical anchor '
                                                                                                 'and forward change; '
                                                                                                 'a cited historical '
                                                                                                 'number does not '
                                                                                                 'certify a forecast.',
                                                                                  'minLength': 1,
                                                                                  'title': 'Reason',
                                                                                  'type': 'string'},
                                                                       'evidence_refs': {'items': {'type': 'string'},
                                                                                         'maxItems': 6,
                                                                                         'title': 'Evidence Refs',
                                                                                         'type': 'array'}},
                                                        'required': ['value', 'basis_type', 'reason', 'evidence_refs'],
                                                        'title': 'ForecastAssumption',
                                                        'type': 'object'},
                                 'ForecastScenario': {'additionalProperties': False,
                                                      'properties': {'scenario_id': {'pattern': '^F[1-3]$',
                                                                                     'title': 'Scenario Id',
                                                                                     'type': 'string'},
                                                                     'name': {'minLength': 1,
                                                                              'title': 'Name',
                                                                              'type': 'string'},
                                                                     'drivers': {'description': 'Business mechanism '
                                                                                                'linking observed '
                                                                                                'activity to the '
                                                                                                'annual forecast; do '
                                                                                                'not merely apply '
                                                                                                'symmetric percentage '
                                                                                                'changes.',
                                                                                 'minLength': 1,
                                                                                 'title': 'Drivers',
                                                                                 'type': 'string'},
                                                                     'revenue': {'$ref': '#/$defs/ForecastAssumption'},
                                                                     'operating_margin': {'$ref': '#/$defs/ForecastAssumption'},
                                                                     'net_nonoperating_income': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                 'description': 'Signed '
                                                                                                                'total '
                                                                                                                'net '
                                                                                                                'interest, '
                                                                                                                'investment, '
                                                                                                                'FX '
                                                                                                                'and '
                                                                                                                'other '
                                                                                                                'pretax '
                                                                                                                'non-operating '
                                                                                                                'income, '
                                                                                                                'in '
                                                                                                                'millions '
                                                                                                                'of '
                                                                                                                'reporting '
                                                                                                                'currency.'},
                                                                     'effective_tax_rate': {'$ref': '#/$defs/ForecastAssumption'},
                                                                     'noncontrolling_attribution': {'$ref': '#/$defs/NoncontrollingAttribution'},
                                                                     'diluted_ordinary_shares': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                 'description': 'Millions '
                                                                                                                'of '
                                                                                                                'diluted '
                                                                                                                'ordinary '
                                                                                                                'shares '
                                                                                                                'for '
                                                                                                                'this '
                                                                                                                'forecast '
                                                                                                                'period, '
                                                                                                                'not '
                                                                                                                'millions '
                                                                                                                'of '
                                                                                                                'ADS.'},
                                                                     'non_working_capital_adjustments': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                         'description': 'Signed '
                                                                                                                        'profit-to-CFO '
                                                                                                                        'adjustments '
                                                                                                                        'before '
                                                                                                                        'changes '
                                                                                                                        'in '
                                                                                                                        'operating '
                                                                                                                        'assets/liabilities, '
                                                                                                                        'including '
                                                                                                                        'noncash '
                                                                                                                        'items. '
                                                                                                                        'CFO '
                                                                                                                        'minus '
                                                                                                                        'net '
                                                                                                                        'income '
                                                                                                                        'ALSO '
                                                                                                                        'includes '
                                                                                                                        'operating '
                                                                                                                        'asset/liability '
                                                                                                                        'cash '
                                                                                                                        'effects '
                                                                                                                        'and '
                                                                                                                        'cannot '
                                                                                                                        'be '
                                                                                                                        'used '
                                                                                                                        'as '
                                                                                                                        'this '
                                                                                                                        'subtotal.'},
                                                                     'operating_asset_liability_cash_effect': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                               'description': 'Signed '
                                                                                                                              'cash-flow '
                                                                                                                              'effects '
                                                                                                                              'of '
                                                                                                                              'changes '
                                                                                                                              'in '
                                                                                                                              'operating '
                                                                                                                              'assets/liabilities, '
                                                                                                                              'including '
                                                                                                                              'tax '
                                                                                                                              'balances '
                                                                                                                              'where '
                                                                                                                              'reported. '
                                                                                                                              'This '
                                                                                                                              'is '
                                                                                                                              'not '
                                                                                                                              'a '
                                                                                                                              'balance-sheet '
                                                                                                                              'change '
                                                                                                                              'or '
                                                                                                                              'necessarily '
                                                                                                                              'strict '
                                                                                                                              'valuation '
                                                                                                                              'NWC; '
                                                                                                                              'cash '
                                                                                                                              'release '
                                                                                                                              'is '
                                                                                                                              'positive, '
                                                                                                                              'use '
                                                                                                                              'is '
                                                                                                                              'negative.'},
                                                                     'cash_capex': {'$ref': '#/$defs/ForecastAssumption',
                                                                                    'description': 'Positive cash '
                                                                                                   'purchases of '
                                                                                                   'PPE/software and '
                                                                                                   'intangibles/content; '
                                                                                                   'deducted once. Do '
                                                                                                   'not claim '
                                                                                                   'maintenance capex '
                                                                                                   'is un-deducted '
                                                                                                   'after including '
                                                                                                   'these purchases.'},
                                                                     'fx_reporting_per_price_currency': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                         'description': 'Units '
                                                                                                                        'of '
                                                                                                                        'reporting '
                                                                                                                        'currency '
                                                                                                                        'for '
                                                                                                                        'ONE '
                                                                                                                        'unit '
                                                                                                                        'of '
                                                                                                                        'quote '
                                                                                                                        'currency, '
                                                                                                                        'e.g. '
                                                                                                                        'CNY '
                                                                                                                        'per '
                                                                                                                        'USD. '
                                                                                                                        'Explicit '
                                                                                                                        'FX '
                                                                                                                        'assumption '
                                                                                                                        'for '
                                                                                                                        'the '
                                                                                                                        'valuation '
                                                                                                                        'date.'},
                                                                     'exit_pe': {'$ref': '#/$defs/ForecastAssumption',
                                                                                 'description': 'Optional annual '
                                                                                                'parent-earnings exit '
                                                                                                'multiple. Justify '
                                                                                                'reference, '
                                                                                                'comparability and '
                                                                                                'risks; null if no '
                                                                                                'defensible basis. A '
                                                                                                'multiple is not a '
                                                                                                'fact or automatically '
                                                                                                'fair value.'},
                                                                     'cash_dividend_per_traded_unit': {'$ref': '#/$defs/ForecastAssumption',
                                                                                                       'description': 'Cumulative '
                                                                                                                      'cash '
                                                                                                                      'dividend '
                                                                                                                      'per '
                                                                                                                      'share/ADS '
                                                                                                                      'over '
                                                                                                                      'the '
                                                                                                                      'holding '
                                                                                                                      'horizon, '
                                                                                                                      'in '
                                                                                                                      'quote '
                                                                                                                      'currency; '
                                                                                                                      'not '
                                                                                                                      'a '
                                                                                                                      'buyback '
                                                                                                                      'amount '
                                                                                                                      'or '
                                                                                                                      'annualized '
                                                                                                                      'yield.'},
                                                                     'valuation_reasoning': {'description': 'Economic '
                                                                                                            'argument '
                                                                                                            'for the '
                                                                                                            'multiple '
                                                                                                            'or why no '
                                                                                                            'multiple '
                                                                                                            'is '
                                                                                                            'defensible; '
                                                                                                            'separate '
                                                                                                            'rerating '
                                                                                                            'from '
                                                                                                            'earnings '
                                                                                                            'growth. '
                                                                                                            'Do not '
                                                                                                            'fit '
                                                                                                            'inputs to '
                                                                                                            'current '
                                                                                                            'price.',
                                                                                             'minLength': 1,
                                                                                             'title': 'Valuation '
                                                                                                      'Reasoning',
                                                                                             'type': 'string'},
                                                                     'evidence_that_changes_case': {'minLength': 1,
                                                                                                    'title': 'Evidence '
                                                                                                             'That '
                                                                                                             'Changes '
                                                                                                             'Case',
                                                                                                    'type': 'string'}},
                                                      'required': ['scenario_id',
                                                                   'name',
                                                                   'drivers',
                                                                   'revenue',
                                                                   'operating_margin',
                                                                   'net_nonoperating_income',
                                                                   'effective_tax_rate',
                                                                   'noncontrolling_attribution',
                                                                   'diluted_ordinary_shares',
                                                                   'non_working_capital_adjustments',
                                                                   'operating_asset_liability_cash_effect',
                                                                   'cash_capex',
                                                                   'fx_reporting_per_price_currency',
                                                                   'exit_pe',
                                                                   'cash_dividend_per_traded_unit',
                                                                   'valuation_reasoning',
                                                                   'evidence_that_changes_case'],
                                                      'title': 'ForecastScenario',
                                                      'type': 'object'},
                                 'NoncontrollingAttribution': {'additionalProperties': False,
                                                               'properties': {'nature': {'description': 'Economic '
                                                                                                        'nature: '
                                                                                                        'profit '
                                                                                                        'attributable '
                                                                                                        'to outside '
                                                                                                        'owners is '
                                                                                                        'deducted; '
                                                                                                        'their loss is '
                                                                                                        'added back. A '
                                                                                                        'statement may '
                                                                                                        'print a '
                                                                                                        'PROFIT '
                                                                                                        'deduction in '
                                                                                                        'parentheses; '
                                                                                                        'parentheses '
                                                                                                        'do not make '
                                                                                                        'it a loss.',
                                                                                         'enum': ['profit',
                                                                                                  'loss',
                                                                                                  'unknown'],
                                                                                         'title': 'Nature',
                                                                                         'type': 'string'},
                                                                              'amount': {'$ref': '#/$defs/AttributionAmount'}},
                                                               'required': ['nature', 'amount'],
                                                               'title': 'NoncontrollingAttribution',
                                                               'type': 'object'}},
                       'additionalProperties': False,
                       'description': 'Business-driven forecast inputs; a calculator, not this model, performs the '
                                      'arithmetic.',
                       'properties': {'business_model': {'description': 'Explain applicability of a non-financial '
                                                                        'operating-company earnings bridge. Do not '
                                                                        'force this template onto banks or loss-making '
                                                                        'early-stage businesses.',
                                                         'minLength': 1,
                                                         'title': 'Business Model',
                                                         'type': 'string'},
                                      'reporting_currency': {'pattern': '^[A-Z]{3}$',
                                                             'title': 'Reporting Currency',
                                                             'type': 'string'},
                                      'price_currency': {'pattern': '^[A-Z]{3}$',
                                                         'title': 'Price Currency',
                                                         'type': 'string'},
                                      'amount_unit': {'const': 'million', 'title': 'Amount Unit', 'type': 'string'},
                                      'earnings_basis': {'description': 'Annual earnings denominator relative to '
                                                                        'valuation date: trailing ends on that date, '
                                                                        'forward starts then or next day, fiscal_year '
                                                                        'uses explicit reporting-year dates and must '
                                                                        'explain comparability.',
                                                         'enum': ['trailing_at_valuation',
                                                                  'forward_from_valuation',
                                                                  'fiscal_year'],
                                                         'title': 'Earnings Basis',
                                                         'type': 'string'},
                                      'forecast_start': {'format': 'date', 'title': 'Forecast Start', 'type': 'string'},
                                      'forecast_end': {'description': 'End of the annual earnings/cash period used at '
                                                                      'valuation; show any actual/forecast blend in '
                                                                      'limitations.',
                                                       'format': 'date',
                                                       'title': 'Forecast End',
                                                       'type': 'string'},
                                      'valuation_date': {'format': 'date', 'title': 'Valuation Date', 'type': 'string'},
                                      'market_price_date': {'anyOf': [{'format': 'date', 'type': 'string'},
                                                                      {'type': 'null'}],
                                                            'title': 'Market Price Date'},
                                      'market_price': {'$ref': '#/$defs/ForecastAssumption'},
                                      'shares_per_traded_unit': {'$ref': '#/$defs/ForecastAssumption',
                                                                 'description': 'Ordinary shares represented by one '
                                                                                'quoted unit; 1 for ordinary shares, '
                                                                                'the source-supported ADS ratio for '
                                                                                'ADRs.'},
                                      'scenarios': {'description': 'Usually 2-3 distinct business paths. Empty only '
                                                                   'when the economic template cannot responsibly be '
                                                                   'applied; explain and still deliver qualitative '
                                                                   'research.',
                                                    'items': {'$ref': '#/$defs/ForecastScenario'},
                                                    'maxItems': 3,
                                                    'title': 'Scenarios',
                                                    'type': 'array'},
                                      'limitations': {'items': {'type': 'string'},
                                                      'maxItems': 8,
                                                      'minItems': 1,
                                                      'title': 'Limitations',
                                                      'type': 'array'}},
                       'required': ['business_model',
                                    'reporting_currency',
                                    'price_currency',
                                    'amount_unit',
                                    'earnings_basis',
                                    'forecast_start',
                                    'forecast_end',
                                    'valuation_date',
                                    'market_price_date',
                                    'market_price',
                                    'shares_per_traded_unit',
                                    'scenarios',
                                    'limitations'],
                       'title': 'UnderwritingDraft',
                       'type': 'object'}}
