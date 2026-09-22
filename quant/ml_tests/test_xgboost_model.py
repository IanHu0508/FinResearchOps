"""Optional ML-runtime integration checks on synthetic data only."""
from dataclasses import replace
import json
import socket
import unittest

from quant.contracts import ContractError
from quant.models.baseline.xgboost_model import XGBoostModel, restore_xgboost_model
from quant.models.views import vector_names, vectors
from quant.pipeline import prepare_dataset
from quant.splits import prepare_fold
from quant.synthetic import make_synthetic_data, synthetic_fold


class XGBoostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data,spec=make_synthetic_data()
        cls.fold=prepare_fold(prepare_dataset(data,spec),synthetic_fold(data))
        cls.model=XGBoostModel().fit(cls.fold.train)

    def test_standard_interface_and_prediction_without_targets(self):
        predictions=self.model.predict(self.fold.test.rows)
        self.assertEqual(tuple(r.key for r in self.fold.test.rows),tuple(p.key for p in predictions))
        self.assertTrue(all(0<=p.predicted_target_percentile<=1 for p in predictions))
        self.assertLess(self.model.training_cutoff.date(),self.fold.window.validation_start)
        self.assertEqual(270,self.model.artifact['training_rows'])
        self.assertAlmostEqual(45,self.model.artifact['date_equal_weight_sum'],places=4)

    def test_native_json_round_trip_and_bounded_vector_path(self):
        doc=json.loads(json.dumps(self.model.artifact))
        restored=restore_xgboost_model(doc)
        expected=self.model.predict(self.fold.test.rows)
        self.assertEqual(expected,restored.predict(self.fold.test.rows))
        actual,raw=restored.predict_vectors(tuple(r.key for r in self.fold.test.rows),
                 vectors(self.fold.test.rows,'tabular','stock-only'),feature_names=vector_names('tabular','stock-only'))
        self.assertEqual(expected,actual)
        self.assertEqual(len(expected),len(raw))

    def test_test_features_do_not_refit_and_context_is_excluded(self):
        before=self.model.artifact
        row=self.fold.test.rows[0]
        altered=replace(row,relative_features=(10,-10),market_context=(0.2,0.9,0.1,0.1),market_context_id='a'*64)
        self.assertEqual(self.model.predict((row,)),self.model.predict((altered,)))
        self.model.predict((replace(row,scalars=(99999,)+row.scalars[1:]),))
        self.assertEqual(before,self.model.artifact)

    def test_duplicate_dates_feature_order_and_tampering_rejected(self):
        with self.assertRaisesRegex(ContractError,'DATE_REPEATED'):
            XGBoostModel().fit_batches((self.fold.train,self.fold.train))
        with self.assertRaisesRegex(ContractError,'FEATURE_DEFINITION'):
            self.model.predict_vectors(tuple(r.key for r in self.fold.test.rows),
                vectors(self.fold.test.rows,'tabular','stock-only'),feature_names=tuple(reversed(vector_names('tabular','stock-only'))))
        doc=self.model.artifact;doc['training_rows']+=1
        with self.assertRaisesRegex(ContractError,'CONTENT_MISMATCH'):
            restore_xgboost_model(doc)

    def test_missing_scalars_remain_native_missing_values(self):
        row=self.fold.test.rows[0]
        altered=replace(row,scalars=(None,)+row.scalars[1:])
        self.assertEqual(1,len(self.model.predict((altered,))))



class IntervalObjectiveTests(unittest.TestCase):
    def test_custom_point_limit_applies_date_weights_exactly_once(self):
        import numpy as np
        import xgboost as xgb
        from quant.models.baseline.xgboost_model import interval_objective,DEFAULT_PARAMETERS
        x=np.array([[0],[1],[2],[3],[4],[5]],dtype=np.float32)
        # Binary-exact targets isolate weighting from label-representation rounding.
        y=np.array([0,.25,.5,.75,1,0],dtype=np.float32)
        weights=np.array([.5,.5,.25,.25,.25,.25],dtype=np.float64)
        params={**DEFAULT_PARAMETERS,'min_child_weight':.01,'max_depth':2,'nthread':1}
        native=xgb.train(params,xgb.DMatrix(x,label=y,weight=weights),num_boost_round=4)
        matrix=xgb.DMatrix(x,weight=weights)
        custom=xgb.train(params,matrix,num_boost_round=4,obj=interval_objective(y,y,weights))
        np.testing.assert_allclose(native.predict(matrix),custom.predict(matrix),rtol=0,atol=1e-7)
        self.assertEqual(0,matrix.get_label().size)

    def test_wide_intervals_have_no_gradient_towards_a_midpoint(self):
        import numpy as np
        import xgboost as xgb
        from quant.models.baseline.xgboost_model import interval_objective
        matrix=xgb.DMatrix(np.array([[0],[1],[2]],dtype=np.float32))
        objective=interval_objective([.2,.2,.2],[.8,.8,.8],[.5,.25,.25])
        g,h=objective(np.array([.3,.7,.9],dtype=np.float32),matrix)
        np.testing.assert_array_equal(g[:2],[0,0]);np.testing.assert_array_equal(h[:2],[0,0])
        self.assertAlmostEqual(.025,float(g[2]),places=7)
        self.assertEqual(.25,float(h[2]))
        matrix.set_label([.5,.5,.5])
        with self.assertRaisesRegex(ContractError,'POINT_LABELS_FORBIDDEN'):
            objective(np.array([.3,.7,.9]),matrix)

if __name__=='__main__':unittest.main()
