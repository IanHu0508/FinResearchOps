# Synthetic Fixtures

Only original, clearly fictional issuer documents belong here. Fixtures must
not reproduce real issuer tables or long passages.

`aurora_revenue_growth_m2.txt` is the one public fixture. It is an original,
fictional `key=value;` fragment bound by source id and SHA-256 to the public
synthetic profile in `src/finauditgate/core/synthetic_profile.py`. Its alias
and adversarial lines exercise fiscal-period, metric, basis, currency, unit,
scale, sign, cutoff, locator, and lineage decisions. It demonstrates the gate's
mechanics; it is not evidence of source authenticity or of generalization to
real filings.
