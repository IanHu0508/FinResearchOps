"""Bounded English HTML reader for January-June consolidated 6-K statements.

Column identity comes from spanned duration/date/currency headers. This reader
supplies facts to the existing cash-flow arithmetic; it never invents XBRL tags.
"""

from datetime import date
from decimal import Decimal, localcontext
import re

from finauditgate.core import cashflow
from finauditgate.core.ixbrl import Filing, FINANCIAL_CONTEXT, _clean


def _grid(rows):
    cells, grid = {}, []
    for ri, row in enumerate(rows):
        col = 0
        for cell in row.descendants():
            if cell.tag not in ("td", "th") or cell.ancestor("tr") is not row:
                continue
            while (ri, col) in cells:
                col += 1
            width, height = int(cell.attrs.get("colspan", 1)), int(cell.attrs.get("rowspan", 1))
            if not 1 <= width <= 128 or not 1 <= height <= 20 or col + width > 128:
                raise ValueError("INTERIM_TABLE_SPAN_UNSUPPORTED")
            for r in range(ri, ri + height):
                for c in range(col, col + width):
                    if (r, c) in cells:
                        raise ValueError("INTERIM_TABLE_SPAN_CONFLICT")
                    cells[r, c] = cell
            col += width
        grid.append([cells.get((ri, c)) for c in range(128)])
    return grid


def _unique(nodes):
    return list({id(n): n for n in nodes if n is not None}.values())


class InterimFiling(Filing):
    def __init__(self, data, task, *, include_profit=False, include_owner_earnings=False):
        super().__init__(data)
        self.include_profit = include_profit
        self.include_owner_earnings = include_owner_earnings
        self._facts, self._concepts, self.tables = {}, {}, {}
        self.all_rows = self.rows
        for table in (n for n in self.nodes if n.tag == "table"):
            rows = [r for r in self.all_rows if r.ancestor("table") is table]
            if not rows:
                continue
            labels = [self.row_label(r).lower() for r in rows]
            kind = ("cashflow" if "net cash provided by operating activities" in labels else
                    "balance" if "total assets" in labels and "contract liabilities" in labels else
                    "tax" if "current income tax" in labels and "deferred taxation" in labels else
                    "operations" if include_profit and "operating profit" in labels and "income before tax" in labels else None)
            if kind is None:
                continue
            if kind in self.tables:
                raise ValueError("INTERIM_DUPLICATE_STATEMENT")
            preceding = [n for n in self.nodes if n.tag in ("p", "h1", "h2", "h3")
                         and n.ancestor("table") is None and n.end <= table.start][-8:]
            heading = " ".join(_clean(n.text()) for n in preceding)
            scale_pattern = r"\(in thousands(?: except[^)]*)?\)" if kind == "operations" else r"\(in thousands\)"
            if not re.search(scale_pattern, heading, re.I):
                raise ValueError("INTERIM_SCALE_HEADER_REQUIRED")
            suffix = {"cashflow": "cash flows", "balance": "balance sheets", "operations": "statements of operations"}.get(kind)
            if suffix and not re.search(r"consolidated.*" + suffix, heading, re.I):
                raise ValueError("INTERIM_CONSOLIDATED_HEADING_REQUIRED")
            self.tables[kind] = (table, rows, self._columns(rows, kind, task), preceding)
        if "cashflow" not in self.tables:
            raise ValueError("INTERIM_CASHFLOW_STATEMENT_REQUIRED")
        # The shared statement finder sees only the consolidated cash-flow table.
        self.rows = self.tables["cashflow"][1]
        self._map_table("cashflow", task)

    def _columns(self, rows, kind, task):
        grid = _grid(rows)
        currencies = {"RMB": "CNY", "CNY": "CNY", "USD": "USD", "US$": "USD"}
        header = next((i for i, row in enumerate(grid[:8])
                       if any(n is not None and _clean(n.text()) in currencies for n in row)), None)
        if header is None:
            raise ValueError("INTERIM_CURRENCY_HEADER_REQUIRED")
        leaves = []
        for cell in _unique(grid[header]):
            if _clean(cell.text()) in currencies:
                indices = [i for i, n in enumerate(grid[header]) if n is cell]
                leaves.append((min(indices), max(indices), cell))
        columns = []
        for pos, (start, last, currency_cell) in enumerate(leaves):
            end = leaves[pos + 1][0] if pos + 1 < len(leaves) else max(i for i, n in enumerate(grid[header]) if n is not None) + 1
            nodes = _unique(grid[r][last] for r in range(header + 1))
            text = " ".join(_clean(n.text()) for n in nodes)
            years = set(re.findall(r"\b20\d{2}\b", text))
            dates = re.findall(r"\b(June|December|March)\s+(\d{1,2})\b", text, re.I)
            if len(years) != 1 or len(set(dates)) != 1:
                raise ValueError("INTERIM_DATE_HEADER_AMBIGUOUS")
            month, day = dates[0]
            end_date = date(int(next(iter(years))), {"june": 6, "december": 12, "march": 3}[month.lower()], int(day))
            duration = 0 if kind == "balance" else 6 if re.search(r"six months ended", text, re.I) else 3 if re.search(r"three months ended", text, re.I) else None
            if duration is None:
                raise ValueError("INTERIM_DURATION_HEADER_REQUIRED")
            columns.append({"start_col": start, "end_col": end, "end": end_date,
                "months": duration, "currency": currencies[_clean(currency_cell.text())], "headers": nodes})
        desired = ([date(task.current_end.year - 1, 12, 31), task.current_end] if kind == "balance"
                   else [task.comparison_end, task.current_end])
        selected = []
        for end_date in desired:
            matches = [c for c in columns if c["end"] == end_date and c["currency"] == task.currency
                       and c["months"] == (0 if kind == "balance" else 6)]
            if len(matches) != 1:
                raise ValueError("INTERIM_COMPARABLE_COLUMN_REQUIRED")
            selected.append(matches[0])
        return grid, header, selected

    def _pair(self, kind, row, task, label=None, measure_kind=None):
        table, rows, (grid, header, columns), preceding = self.tables[kind]
        ri = rows.index(row)
        if ri <= header:
            return []
        result = []
        label = label or self.row_label(row)
        for column in columns:
            nodes = _unique(grid[ri][column["start_col"]:column["end_col"]])
            raw = "".join(_clean(n.text()) for n in nodes).strip()
            if not re.fullmatch(r"(?:-?(?:\d{1,3}(?:,\d{3})+|\d+)|\((?:\d{1,3}(?:,\d{3})+|\d+)\))", raw):
                raise ValueError("INTERIM_MISSING_OR_UNSUPPORTED_NUMBER")
            with localcontext(FINANCIAL_CONTEXT):
                amount = Decimal(raw.replace(",", "").replace("(", "-").replace(")", "")) * 1000
            node = next(n for n in nodes if re.search(r"\d", n.text()))
            fact = {"fact_id": f"fact-{node.start}", "concept": "html:" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_"),
                "context_id": f"{kind}-{column['months']}m-{column['end']}-{column['currency']}",
                "currency": column["currency"], "value": format(amount, "f"), "displayed_cash_effect": format(amount, "f"),
                "period_start": "" if kind == "balance" else date(column["end"].year, 1, 1).isoformat(),
                "period_end": column["end"].isoformat(), "printed": raw, "uncertainty": "500",
                "row_label": label, "source": self.reference(row), "source_kind": "HTML_TABLE_CELL",
                "source_headers": [self.reference(n) for n in column["headers"]],
                "scale_sources": [self.reference(n) for n in preceding if "in thousands" in n.text().lower()],
                "measurement_kind": measure_kind or "HALF_YEAR_RECONCILIATION_COMPONENT"}
            result.append((node, fact))
        return result

    def _map_table(self, kind, task):
        rows = self.tables[kind][1]
        start = next(i for i, r in enumerate(rows) if self.row_label(r).lower() == "net income")
        end = next(i for i, r in enumerate(rows) if self.row_label(r).lower() == "net cash provided by operating activities")
        if start >= end:
            raise ValueError("INTERIM_CASHFLOW_ORDER_INVALID")
        for row in rows[start:end + 1]:
            label = self.row_label(row)
            if label.lower().startswith(("adjustments to reconcile", "changes in operating assets")) or not label:
                continue
            pair = self._pair(kind, row, task)
            concept = "ProfitLoss" if row is rows[start] else "NetCashProvidedByUsedInOperatingActivities" if row is rows[end] else "Adjustment"
            for node, fact in pair:
                if concept != "Adjustment":
                    fact["measurement_kind"] = "HALF_YEAR_NET_INCOME" if concept == "ProfitLoss" else "HALF_YEAR_OPERATING_CASH_FLOW"
                self._concepts[id(node)] = concept
            self._facts[id(row)] = pair

    def row_facts(self, row, task):
        return self._facts.get(id(row), [])

    def is_concept(self, node, name):
        return self._concepts.get(id(node)) == name

    def displayed_effect(self, row, node, fact):
        return fact["value"]

    def counterpart(self, *args):
        return None

    def search_notes(self, label, limit=3, current_year=None):
        hits = super().search_notes(label, limit=12, current_year=current_year)
        for hit in hits:
            text = hit["text"].lower()
            # Year-only relevance cannot establish the half-year period.
            half = bool(re.search(r"six months|first half", text))
            quarter = bool(re.search(r"quarter|three months", text))
            hit["period_basis"] = "EXPLICIT_HALF_YEAR" if half and not quarter else "SUBANNUAL_PERIOD_NOT_ESTABLISHED"
            if not half or quarter:
                if hit["evidence_role"] in ("PERIOD_FACT", "PERIOD_CHANGE_EXPLANATION"):
                    hit["evidence_role"] = "OTHER_PERIOD" if quarter else "RELATED_CONTEXT"
        hits.sort(key=lambda h: (h["period_basis"] != "EXPLICIT_HALF_YEAR", h["evidence_role"] == "OTHER_PERIOD"))
        if self.include_profit:
            # Current-period financial explanations may inherit a dated section
            # heading. Quarter-only or unresolved prose is not forwarded as proof.
            for hit in hits:
                section = None
                for node in self.nodes:
                    if node.start >= hit["source"]["char_start"]:
                        break
                    if node.tag not in ("p", "h1", "h2", "h3") or node.ancestor("tr") is not None:
                        continue
                    text = _clean(node.text()).lower()
                    if len(text) > 160 or not re.search(r"financial (results|highlights)$", text):
                        continue
                    if re.match(r"six months ended|first half", text):
                        section = ("HALF_YEAR" if str(current_year) in text else "OTHER", self.reference(node))
                    elif re.match(r"(?:first|second|third|fourth) quarter", text):
                        section = ("QUARTER", self.reference(node))
                text = hit["text"].lower()
                if section and section[0] == "HALF_YEAR" and not re.search(r"quarter|three months", text):
                    hit["period_basis"] = "HALF_YEAR_SECTION"
                    hit["period_section_source"] = section[1]
                    if hit["evidence_role"] == "RELATED_CONTEXT":
                        hit["evidence_role"] = "PERIOD_CHANGE_EXPLANATION" if re.search(r"attribut|mainly due|driven", text) else "PERIOD_FACT"
                elif section and section[0] in ("QUARTER", "OTHER"):
                    hit["period_basis"] = "SUBANNUAL_PERIOD_NOT_ESTABLISHED"
            hits = [h for h in hits if h["period_basis"] in ("EXPLICIT_HALF_YEAR", "HALF_YEAR_SECTION")
                    and h["evidence_role"] != "OTHER_PERIOD"]
        return hits[:limit]

    def profit_bridge(self, task, analysis):
        if "operations" not in self.tables:
            return None, []
        rows = self.tables["operations"][1]
        labels = (("operating_profit", "Operating profit"), ("investment", "Investment income/(loss), net"),
                  ("interest", "Interest income, net"), ("exchange", "Exchange gains/(losses), net"),
                  ("other", "Other, net"), ("pretax", "Income before tax"), ("tax", "Income tax"),
                  ("net_income", "Net income"))
        values, facts = {}, []
        for key, label in labels:
            candidates = [r for r in rows if self.row_label(r).lower() == label.lower()]
            if not candidates:
                raise ValueError("PROFIT_BRIDGE_ROW_MISSING")
            pairs = [[f for _, f in self._pair("operations", row, task,
                      measure_kind="HALF_YEAR_INCOME_STATEMENT_VALUE")] for row in candidates]
            if len({tuple(f["value"] for f in pair) for pair in pairs}) != 1:
                raise ValueError("PROFIT_BRIDGE_DUPLICATE_CONFLICT")
            previous, current = pairs[0]
            facts.extend(pairs[0])
            values[key] = {"label": label, "current": current["value"], "comparison": previous["value"],
                "change": cashflow._difference(current["value"], previous["value"]),
                "fact_ids": [current["fact_id"], previous["fact_id"]]}
        components = ("operating_profit", "investment", "interest", "exchange", "other", "tax")
        residuals = {}
        with localcontext(FINANCIAL_CONTEXT):
            for period in ("current", "comparison"):
                residual = sum((Decimal(values[k][period]) for k in components), Decimal(0)) - Decimal(values["net_income"][period])
                pretax_residual = sum((Decimal(values[k][period]) for k in components if k != "tax"), Decimal(0)) - Decimal(values["pretax"][period])
                residuals[period] = format(residual, "f")
                if abs(residual) > 3500 or abs(pretax_residual) > 3000:
                    raise ValueError("PROFIT_BRIDGE_UNRECONCILED")
                if values["net_income"][period] != analysis["metrics"]["profit"][period]:
                    raise ValueError("PROFIT_CASHFLOW_NET_INCOME_CONFLICT")
                tax = analysis.get("supplemental", {}).get("total_tax_expense")
                if tax and Decimal(values["tax"][period]) != -Decimal(tax[period]):
                    raise ValueError("PROFIT_TAX_EXPENSE_CONFLICT")
        return {"components": [{"key": k, **values[k]} for k in components],
                "pretax": values["pretax"], "net_income": values["net_income"],
                "residuals": residuals, "assurance": "ACCOUNTING_CHANGE_DECOMPOSITION_NOT_BUSINESS_CAUSAL_PROOF"}, facts

    def earnings_attribution(self, task, analysis):
        """Reconcile signed deductions, including redeemable-interest accretion."""
        if "operations" not in self.tables:
            raise ValueError("OWNER_EARNINGS_STATEMENT_REQUIRED")
        labels = (("parent", "Net income attributable to the Company’s shareholders"),
                  ("noncontrolling_deduction", "Net income attributable to noncontrolling interests"),
                  ("accretion_deduction", "Accretion of redeemable noncontrolling interests"))
        values, facts = {}, []
        normalize = lambda x: x.lower().replace("’", "'")
        for key, label in labels:
            rows = [r for r in self.tables["operations"][1] if normalize(self.row_label(r)) == normalize(label)]
            if len(rows) != 1:
                raise ValueError("OWNER_EARNINGS_ROW_REQUIRED")
            previous, current = [f for _, f in self._pair("operations", rows[0], task,
                measure_kind="HALF_YEAR_PARENT_NET_INCOME" if key == "parent" else "HALF_YEAR_SIGNED_ATTRIBUTION_ADJUSTMENT")]
            facts.extend((previous, current))
            values[key] = {"label": label, "current": current["value"], "comparison": previous["value"],
                "change": cashflow._difference(current["value"], previous["value"]),
                "fact_ids": [current["fact_id"], previous["fact_id"]]}
        residuals = {}
        with localcontext(FINANCIAL_CONTEXT):
            for period in ("current", "comparison"):
                residual = (Decimal(analysis["metrics"]["profit"][period])
                    + Decimal(values["noncontrolling_deduction"][period])
                    + Decimal(values["accretion_deduction"][period]) - Decimal(values["parent"][period]))
                if abs(residual) > 2000:
                    raise ValueError("OWNER_EARNINGS_UNRECONCILED")
                residuals[period] = format(residual, "f")
        return {**values, "residuals": residuals,
            "basis": "CONSOLIDATED_NET_INCOME_PLUS_SIGNED_ATTRIBUTION_ADJUSTMENTS_EQUALS_PARENT_NET_INCOME",
            "use_limit": "CFO reconciles consolidated net income, not parent income. Neither income figure is EPS; no annualization, FX or ADS earnings conversion is supplied."}, facts

    def supplement(self, task):
        result, facts = {}, []
        requests = (("cashflow", "cash paid for income taxes, net", "cash_income_taxes_paid", "HALF_YEAR_CASH_TAX_PAYMENT"),
                    ("balance", "contract liabilities", "contract_liability_balance", "INSTANT_LIABILITY_BALANCE"),
                    ("tax", "current income tax", "current_tax_expense", "HALF_YEAR_TAX_EXPENSE"),
                    ("tax", "deferred taxation", "deferred_tax_expense", "HALF_YEAR_TAX_EXPENSE"))
        for table, label, key, kind in requests:
            if table not in self.tables:
                result[key] = None
                continue
            matches = [r for r in self.tables[table][1] if self.row_label(r).lower() == label]
            if len(matches) != 1:
                raise ValueError("INTERIM_SUPPLEMENT_ROW_AMBIGUOUS")
            pair = [f for _, f in self._pair(table, matches[0], task, measure_kind=kind)]
            facts.extend(pair)
            previous, current = pair
            result[key] = {"comparison": previous["value"], "current": current["value"],
                "change": cashflow._difference(current["value"], previous["value"]),
                "fact_ids": [current["fact_id"], previous["fact_id"]],
                "comparison_end": previous["period_end"], "current_end": current["period_end"], "measure_kind": kind}
        if result["current_tax_expense"] and result["deferred_tax_expense"]:
            with localcontext(FINANCIAL_CONTEXT):
                result["total_tax_expense"] = {p: format(sum(Decimal(result[k][p]) for k in ("current_tax_expense", "deferred_tax_expense")), "f") for p in ("current", "comparison")}
            result["total_tax_expense"]["fact_ids"] = result["current_tax_expense"]["fact_ids"] + result["deferred_tax_expense"]["fact_ids"]
            tax_rows = self.tables["tax"][1]
            last_component = max(r.start for r in tax_rows if self.row_label(r).lower() in ("current income tax", "deferred taxation"))
            totals = [r for r in tax_rows if r.start > last_component and re.search(r"\d", r.text())
                      and not re.search(r"[A-Za-z]", self.row_label(r))]
            if len(totals) != 1:
                raise ValueError("INTERIM_TAX_TOTAL_REQUIRED")
            total_facts = [f for _, f in self._pair("tax", totals[0], task,
                label="Income tax expense: reported component total", measure_kind="HALF_YEAR_TAX_EXPENSE")]
            if any(result["total_tax_expense"][p] != f["value"] for p, f in zip(("comparison", "current"), total_facts)):
                raise ValueError("INTERIM_TAX_TOTAL_MISMATCH")
            facts.extend(total_facts)
            result["total_tax_expense"]["component_fact_ids"] = result["total_tax_expense"]["fact_ids"]
            result["total_tax_expense"]["fact_ids"] = [f["fact_id"] for f in reversed(total_facts)]
        else:
            result["total_tax_expense"] = None
        return result, facts


def analyze(filing, task):
    result = cashflow.analyze(filing, task)
    result["schema_version"] = "finresearchops.cashflow-analysis/v5" if filing.include_profit else "finresearchops.cashflow-analysis/v4"
    if filing.include_owner_earnings:
        result["schema_version"] = "finresearchops.cashflow-analysis/v6"
    result["period_months"] = 6
    if result["metrics"]:
        try:
            result["supplemental"], facts = filing.supplement(task)
            result["facts"].extend(facts)
            if filing.include_profit:
                result["profit_bridge"], facts = filing.profit_bridge(task, result)
                result["facts"].extend(facts)
            if filing.include_owner_earnings:
                result["earnings_attribution"], facts = filing.earnings_attribution(task, result)
                result["facts"].extend(facts)
        except ValueError as exc:
            result["issues"].append(str(exc))
        # A partial plain-table parse may not feed a model as audited facts.
        if result["issues"]:
            result["metrics"] = {}
            result["status"] = "PARTIAL"
    return result
