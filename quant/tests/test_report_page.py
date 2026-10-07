"""A report can render without unpublished Agent modules or optional libraries."""

import os
from pathlib import Path
import subprocess
import sys
import unittest

from quant.report_page import page


class ReportPageTests(unittest.TestCase):
    def test_source_text_is_escaped_in_titles_tables_and_code(self):
        result = page('# Synthetic <script>\n\n| Value |\n| --- |\n| <img> |\n\n```\n<script>\n```\n', '<script>').decode()
        self.assertNotIn('<script>', result)
        self.assertNotIn('<img>', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('<table>', result)
        self.assertIn('<pre>', result)

    def test_quant_renderer_has_no_agent_or_numerical_runtime_dependency(self):
        root = Path(__file__).resolve().parents[2]
        code = ('import sys;from quant.report_page import page;'
                "assert b'SYNTHETIC_ONLY' in page('# SYNTHETIC_ONLY', 'Synthetic');"
                "assert not any(n in sys.modules for n in ('finauditgate','numpy','scipy','xgboost','tradingagents'))")
        result = subprocess.run([sys.executable, '-S', '-c', code], cwd=root,
            env={**os.environ, 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
