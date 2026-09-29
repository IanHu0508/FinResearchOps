"""JSON Schemas of the protocol 23 stages that differ from v18, exported from the Pydantic models; do not edit by hand.

Protocol 23 removes REVIEW from the research manager's recommendation and the final rating and adds the
final confidence. Every other stage keeps its v18 schema. Integration tests require these to equal the live models.
"""

SCHEMAS_V23 = {'ResearchEvaluation': {'$defs': {'Assessment': {'additionalProperties': False,
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
                                                                                                    'analysis under '
                                                                                                    'its stated '
                                                                                                    'assumptions. When '
                                                                                                    'the basis is '
                                                                                                    'thin, say so in '
                                                                                                    'the rationale '
                                                                                                    'instead of '
                                                                                                    'withholding a '
                                                                                                    'rating; Hold '
                                                                                                    'means a supported '
                                                                                                    'neutral view.',
                                                                                     'enum': ['Buy',
                                                                                              'Overweight',
                                                                                              'Hold',
                                                                                              'Underweight',
                                                                                              'Sell'],
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
                         'properties': {'rating': {'description': "Research conclusion under this report's scenarios "
                                                                  'and assumptions, not a certified fair value. '
                                                                  'Missing consensus, comparables or valuation history '
                                                                  'lowers confidence; it never withholds the rating.',
                                                   'enum': ['Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'],
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
                                                                'type': 'array'},
                                        'confidence': {'description': 'How firmly the cited evidence supports the '
                                                                      'rating.',
                                                       'enum': ['high', 'medium', 'low'],
                                                       'title': 'Confidence',
                                                       'type': 'string'}},
                         'required': ['rating',
                                      'summary',
                                      'financial_analysis',
                                      'strongest_counterevidence',
                                      'scenario_assessments',
                                      'limitations',
                                      'change_explanations',
                                      'belief_explanations',
                                      'confidence'],
                         'title': 'FinalResearchReport',
                         'type': 'object'}}
