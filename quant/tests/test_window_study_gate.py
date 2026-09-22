"""Study-protocol checks, separately from actual frozen-file admission tests."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from quant.contracts import ContractError
from quant.data.freeze import load_frozen_study_review


def registered_folds():
    result=[]
    for year in range(2018,2024):
        for name,start in (('rolling_2y',year-2),('rolling_5y',year-5),('expanding',2010)):
            result.append({'candidate':name,'evaluation_year':year,'fit_cutoff':f'{year}-01-01T00:00:00+08:00',
                'training_score_start':f'{start}-01-01','training_score_end_exclusive':f'{year}-01-01',
                'evaluation_score_start':f'{year}-01-01','evaluation_score_end':f'{year}-12-31',
                'development_label_cutoff_exclusive':'2024-01-01T00:00:00+08:00'})
    return result


class WindowStudyGateTests(unittest.TestCase):
    def test_only_eighteen_registered_windows_and_mature_development_bounds(self):
        folds=registered_folds()
        # Underlying canonical/evidence admission is covered without mocking in
        # test_interval_admission. Here isolate the additional annual contract.
        report={'view':{'folds':folds},'coverage':[]}
        with patch('quant.data.freeze._load_frozen_review',return_value=report):
            self.assertIs(report,load_frozen_study_review({},'unused',folds))
        for field,value in [('training_score_start','2011-01-01'),
                            ('training_score_end_exclusive','2018-02-01'),
                            ('fit_cutoff','2018-01-02T00:00:00+08:00'),
                            ('development_label_cutoff_exclusive','2025-01-01T00:00:00+08:00')]:
            changed=deepcopy(folds);changed[0][field]=value
            with self.subTest(field=field),patch('quant.data.freeze._load_frozen_review',return_value={'view':{'folds':changed}}):
                with self.assertRaisesRegex(ContractError,'DATE_RULE_CHANGED'):
                    load_frozen_study_review({},'unused',changed)

    def test_extra_window_or_final_holdout_cannot_be_dispatched(self):
        for altered in (registered_folds()+[registered_folds()[0]],registered_folds()):
            if len(altered)==18:altered[-1]['evaluation_year']=2024
            with patch('quant.data.freeze._load_frozen_review',return_value={'view':{'folds':altered}}):
                with self.assertRaisesRegex(ContractError,'REGISTERED_18'):
                    load_frozen_study_review({},'unused',altered)

    def test_annual_runner_does_not_restore_boolean_admission(self):
        with self.assertRaisesRegex(ContractError,'FROZEN_BUNDLE_REQUIRED'):
            load_frozen_study_review({'phase_a_accepted':True},'unused',registered_folds())
