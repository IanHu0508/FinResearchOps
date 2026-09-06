"""Bounded, offline reader for annual, consolidated inline-XBRL cash flows.

This is a deliberately limited reader, not an XBRL validator. Unsupported
contexts/formats never become guessed numbers. Cash-flow effects are read
from the *presentation sign*, which can differ from the taxonomy fact sign.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from html.parser import HTMLParser
import re

from finauditgate.core.artifacts import sha256_hex


FINANCIAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "param", "source", "track"}
_INVISIBLE = {"ix:hidden", "ix:header", "head", "script", "style"}


@dataclass
class _Node:
    tag: str
    attrs: dict
    start: int
    end: int = 0
    parent: "_Node | None" = None
    children: list = field(default_factory=list)

    def descendants(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.descendants()

    def text(self) -> str:
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children)

    def ancestor(self, tag: str):
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return node
            node = node.parent
        return None

    def hidden(self) -> bool:
        node = self
        while node is not None:
            if node.tag in _INVISIBLE:
                return True
            node = node.parent
        return False


class _Parser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.offsets = [0]
        for match in re.finditer("\n", source):
            self.offsets.append(match.end())
        self.root = _Node("root", {}, 0, len(source))
        self.stack = [self.root]
        self.nodes = []

    def position(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 180 or len(self.nodes) >= 350_000:
            raise ValueError("HTML_COMPLEXITY_UNSUPPORTED")
        node = _Node(tag, dict(attrs), self.position(), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag in _VOID:
            node.end = node.start + len(self.get_starttag_text())
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop().end = self.position() + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                end = self.source.find(">", self.position()) + 1
                for node in self.stack[i:]:
                    node.end = end
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _local(tag):
    return tag.rsplit(":", 1)[-1]


def _clean(text):
    return " ".join(text.split())


def _decimal(node):
    attrs = node.attrs
    if attrs.get("xsi:nil") in ("true", "1") or attrs.get("continuedat"):
        raise ValueError("NIL_OR_CONTINUED_FACT_UNSUPPORTED")
    if any(n.tag == "ix:nonfraction" for n in node.descendants()):
        raise ValueError("NESTED_FACT_UNSUPPORTED")
    form = attrs.get("format", "").rsplit(":", 1)[-1]
    raw = _clean(node.text())
    if form in ("fixed-zero", "zerodash") and raw in ("—", "–", "-", "0"):
        plain = "0"
    elif form in ("num-dot-decimal", "numdotdecimal", ""):
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", raw):
            raise ValueError("NUMBER_FORMAT_UNSUPPORTED")
        plain = raw.replace(",", "")
    else:
        raise ValueError("NUMBER_TRANSFORM_UNSUPPORTED")
    scale = int(attrs.get("scale", "0"))
    if not -12 <= scale <= 12 or len(plain) > 35 or attrs.get("sign", "") not in ("", "-"):
        raise ValueError("NUMBER_RANGE_UNSUPPORTED")
    decimals = attrs.get("decimals", "INF")
    if decimals != "INF" and not re.fullmatch(r"-?\d{1,2}", decimals):
        raise ValueError("DECIMALS_UNSUPPORTED")
    with localcontext(FINANCIAL_CONTEXT):
        value = Decimal(plain) * (Decimal(10) ** scale)
        if attrs.get("sign") == "-":
            value = -value
        uncertainty = Decimal(0) if decimals == "INF" else Decimal("0.5") * Decimal(10) ** -int(decimals)
    return value, uncertainty


class Filing:
    def __init__(self, data: bytes):
        self.sha256 = sha256_hex(data)
        self.source = data.decode("utf-8", errors="strict")
        parser = _Parser(self.source)
        parser.feed(self.source)
        parser.close()
        self.nodes = parser.nodes
        self.contexts = {}
        self.units = {}
        self.namespace = {}
        for node in self.nodes:
            for key, value in node.attrs.items():
                if key.startswith("xmlns:"):
                    self.namespace[key[6:]] = value
            local = _local(node.tag)
            if local == "context":
                values = {}
                for child in node.descendants():
                    name = _local(child.tag)
                    if name in ("identifier", "startdate", "enddate", "instant"):
                        values[name] = child.text().strip()
                    if name in ("segment", "scenario"):
                        values["dimensional"] = True
                key = node.attrs.get("id")
                if key in self.contexts:
                    raise ValueError("DUPLICATE_CONTEXT_ID")
                self.contexts[key] = values
            elif local == "unit":
                measures = [c.text().strip() for c in node.descendants() if _local(c.tag) == "measure"]
                key = node.attrs.get("id")
                if key in self.units:
                    raise ValueError("DUPLICATE_UNIT_ID")
                self.units[key] = measures[0] if len(measures) == 1 and not any(_local(c.tag) == "divide" for c in node.descendants()) else None
        self.rows = [n for n in self.nodes if n.tag == "tr" and not n.hidden()]

    def reference(self, node):
        raw = self.source[node.start:node.end]
        return {"document_sha256": self.sha256, "char_start": node.start,
                "char_end": node.end, "span_sha256": sha256_hex(raw.encode("utf-8")),
                "anchor": node.attrs.get("id", "")}

    def monetary_fact(self, node, task):
        if node.tag != "ix:nonfraction" or node.hidden():
            return None
        context = self.contexts.get(node.attrs.get("contextref"), {})
        if context.get("dimensional") or "instant" in context:
            return None
        if context.get("identifier", "").lstrip("0") != task.entity_identifier.lstrip("0"):
            return None
        measure = self.units.get(node.attrs.get("unitref"))
        if not measure or ":" not in measure:
            return None
        prefix, currency = measure.split(":", 1)
        if currency != task.currency or self.namespace.get(prefix) != "http://www.xbrl.org/2003/iso4217":
            return None
        try:
            start = date.fromisoformat(context["startdate"])
            end = date.fromisoformat(context["enddate"])
        except (KeyError, ValueError):
            return None
        if end not in (task.current_end, task.comparison_end) or not 330 <= (end - start).days <= 400:
            return None
        value, uncertainty = _decimal(node)
        return {"fact_id": f"fact-{node.start}", "concept": node.attrs.get("name", ""),
                "period_start": start.isoformat(), "period_end": end.isoformat(),
                "currency": currency, "value": format(value, "f"),
                "uncertainty": format(uncertainty, "f"), "context_id": node.attrs.get("contextref"),
                "printed": _clean(node.text()), "source": self.reference(node)}

    def is_concept(self, node, local):
        concept = node.attrs.get("name", "")
        if ":" not in concept:
            return False
        prefix, name = concept.split(":", 1)
        return name == local and bool(re.fullmatch(r"https?://fasb.org/us-gaap/\d{4}", self.namespace.get(prefix, "")))

    def row_facts(self, row, task):
        return [(n, self.monetary_fact(n, task)) for n in row.descendants()
                if n.tag == "ix:nonfraction" and n.ancestor("tr") is row]

    def displayed_effect(self, row, target, fact):
        parts = []
        marker = "\x00FACT\x00"
        def visit(node):
            if node is target:
                parts.append(marker)
                return
            for child in node.children:
                if isinstance(child, _Node):
                    visit(child)
                else:
                    parts.append(child)
        visit(row)
        before, after = "".join(parts).split(marker, 1)
        negative = (before.rstrip().endswith("(") and after.lstrip().startswith(")")) or before.rstrip().endswith("-")
        with localcontext(FINANCIAL_CONTEXT):
            magnitude = abs(Decimal(fact["value"]))
            return format(-magnitude if negative else magnitude, "f")

    def row_label(self, row):
        for cell in row.descendants():
            if cell.tag not in ("td", "th") or cell.ancestor("tr") is not row:
                continue
            text = _clean(cell.text())
            if re.search(r"[A-Za-z]", text) and not any(n.tag == "ix:nonfraction" for n in cell.descendants()):
                return text[:240]
        def visible(node):
            if node.tag == "ix:nonfraction":
                return " "
            return "".join(visible(c) if isinstance(c, _Node) else c for c in node.children)
        text = _clean(visible(row))
        # Strip separate numeric-cell punctuation, preserving parentheses that
        # belong to the label, e.g. "Other non-cash expenses (income)".
        text = re.sub(r"(?:\s+[()$¥,—–-]+)+$", "", text)
        return text.strip(" $¥,—–-")[:240]

    def counterpart(self, present, expected, task, row):
        """Resolve a missing period from the same filed concept and full context.

        A blank/dash in the cash-flow table is never evidence of zero. A zero
        explicitly tagged elsewhere in this filing is evidence of a reported
        zero. Nonzero effects also require a sign relation visible in this row.
        """
        candidates = []
        for node in self.nodes:
            if node.tag != 'ix:nonfraction' or node.attrs.get('name') != present['concept']:
                continue
            fact = self.monetary_fact(node, task)
            if fact is not None and (fact['period_start'], fact['period_end']) == (expected['period_start'], expected['period_end']):
                candidates.append(fact)
        if not candidates:
            return None
        with localcontext(FINANCIAL_CONTEXT):
            best = min(candidates, key=lambda f: (Decimal(f['uncertainty']), f['source']['char_start']))
            value = Decimal(best['value'])
            if len({Decimal(f['value']) for f in candidates if Decimal(f['uncertainty']) == Decimal(best['uncertainty'])}) != 1:
                raise ValueError('COUNTERPART_FACT_CONFLICT')
            if any(abs(value - Decimal(f['value'])) > Decimal(f['uncertainty']) + Decimal(best['uncertainty']) for f in candidates):
                raise ValueError('COUNTERPART_FACT_CONFLICT')
            basis, effect = 'EXPLICIT_TAGGED_ZERO', Decimal(0)
            if value != 0:
                anchor_value, anchor_effect = Decimal(present['value']), Decimal(present['displayed_cash_effect'])
                if anchor_value == 0 or abs(anchor_value) != abs(anchor_effect):
                    raise ValueError('COUNTERPART_CASH_SIGN_UNRESOLVED')
                multiplier = Decimal(1) if anchor_value == anchor_effect else Decimal(-1)
                effect, basis = value * multiplier, 'MATCHED_CONCEPT_WITH_CASHFLOW_SIGN'
        return {**best, 'displayed_cash_effect': format(effect, 'f'), 'row_label': self.row_label(row),
                'resolution': {'method': basis, 'cashflow_row_source': self.reference(row),
                               'sign_reference_fact_id': present['fact_id'],
                               'supporting_sources': [f['source'] for f in candidates]}}

    def note_passages(self):
        if hasattr(self, '_passages'):
            return self._passages
        passages, heading, heading_source = [], '', None
        for node in self.nodes:
            if node.tag not in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6') or node.hidden():
                continue
            if any(n.tag in ('p', 'div') for n in node.descendants()):
                continue
            text = _clean(node.text())
            bold = any(n.tag in ('b', 'strong') or re.search(r'font-weight\s*:\s*(?:bold|[7-9]00)', n.attrs.get('style', '')) for n in (node, *node.descendants()))
            if node.ancestor('tr') is None and (node.tag.startswith('h') or (len(text) <= 140 and bold)):
                heading, heading_source = text, self.reference(node)
                continue
            if not 80 <= len(text) <= 6000:
                continue
            passages.append({'note_id': f'note-{node.start}', 'text': text,
                             'source': self.reference(node), 'heading': heading,
                             'heading_source': heading_source})
        self._passages = passages
        return passages

    def search_notes(self, label, limit=3, current_year=None):
        from finauditgate.core.disclosures import rank
        return rank(self.note_passages(), label, current_year, limit)
