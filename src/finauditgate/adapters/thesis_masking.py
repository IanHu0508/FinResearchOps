"""Protocol 24: hide the numbers the contract refuses instead of discarding the final report.

When the final report is refused only for unbound numbers and the sentence repair is unavailable
or does not pass, the report keeps its words, citations, rating and scenario dispositions. The
contract reads each stretch of prose between two citations on its own, so masking works on the
same stretches. In a refused stretch every numeral-like span (a standalone numeral, a numeral
attached to letters such as 5G or Q3 or another numeric character such as ①, Chinese numerals and
amounts, English number words) is first hidden as a pending mark; then, sentence by sentence and left to right,
spans are shown again when the stretch still passes with them (a whole sentence's spans at once
when that passes, otherwise one span at a time). Text that fails even with every span hidden,
judged sentence by sentence after the text before it, is replaced by one mark. The masked report
must pass the unchanged contract, or the run stops as before. The saved answer stays in the Case,
and the reader derives the same masking from it.
"""

from copy import deepcopy
import re


MARK = "〔数值待核〕"
SENTENCE_MARK = "〔此处含未核数值，已隐去〕"
# A standalone numeral: digits with decimals, grouping, a sign, a percent or a following 万/亿/千/百.
_NUMBER = re.compile(r"(?<![A-Za-z0-9０-９_.．])[+\-−]?[0-9０-９](?:[0-9０-９,，.．]*[0-9０-９])?"
                     r"(?:[%％]|[万亿千百]+|(?![.．,，][0-9０-９]))(?![A-Za-z0-9０-９_])")
# A run of non-CJK word characters (a dot allowed between digits); spots are the runs holding a numeral,
# which covers numerals attached to letters (5G, Q3, 10EMA) and other numeric characters (①, ½).
_GLUED = re.compile(r"(?:[^\W\u3040-\u9fff]|(?<=[0-9０-９])[.．](?=[0-9０-９]))+")
_CN_RUN = re.compile(r"[零〇一二两三四五六七八九十百千万亿兆壹贰叁肆伍陆柒捌玖拾佰仟]+")
_END = re.compile(r"[。！？!?\n]+$")


def _number_words():
    from finauditgate.application.research_narrative import _EN_AMOUNT
    words = _EN_AMOUNT.pattern
    if not (words.startswith(r"\b(?:") and words.endswith(r")\b")):
        raise ValueError("THESIS_MASKING_NUMBER_WORDS_CHANGED")
    # Next to Chinese text a number word has no word boundary, but hiding its neighbour would expose it.
    return re.compile(r"(?<![A-Za-z])" + words[2:-2] + r"(?![A-Za-z])", re.I)


def _spots(text):
    """Numeral-like spans in prose, earlier kinds first and without overlaps."""
    from finauditgate.application.research_narrative import _CN_AMOUNT
    spans = [m.span() for m in _NUMBER.finditer(text)]
    extra = [m.span() for m in _GLUED.finditer(text) if any(c.isnumeric() for c in m[0])]
    extra += [(m.start(), m.end() + (m.end() < len(text) and text[m.end()] in "半多余")) for m in _CN_AMOUNT.finditer(text)]
    extra += [m.span() for pattern in (_CN_RUN, _number_words()) for m in pattern.finditer(text)]
    for start, end in extra:
        if not any(s < end and start < e for s, e in spans):
            spans.append((start, end))
    return sorted(spans)


def _masked_stretch(stretch, refused):
    """(masked stretch, offsets of its whole-text marks) for one refused stretch; None when it cannot pass."""
    from .thesis_repair import sentences
    parts = sentences(stretch)
    spots = [(k, span) for k, part in enumerate(parts) for span in _spots(part)]
    whole = set()

    def build(shown, upto=None):
        out, marks, length = [], [], 0
        for k, part in enumerate(parts[:upto]):
            if k in whole:
                end = _END.search(part)
                part = SENTENCE_MARK + (end[0] if end else "")
                marks.append(length)
            else:
                for j in reversed(range(len(spots))):  # right to left, so earlier offsets stay valid
                    if spots[j][0] == k and j not in shown:
                        start, stop = spots[j][1]
                        part = part[:start] + MARK + part[stop:]
            out.append(part)
            length += len(part)
        return "".join(out), marks

    def fails(shown, upto=None):
        return refused(build(shown, upto)[0])

    for k in range(len(parts)):
        if fails(set(), k + 1):
            whole.add(k)
            if fails(set(), k + 1):
                return None
    if fails(set()):
        return None
    shown = set()
    for k in range(len(parts)):
        group = set() if k in whole else {j for j, (spot_part, _) in enumerate(spots) if spot_part == k}
        if not group:
            continue
        if not fails(shown | group):
            shown |= group
            continue
        for j in sorted(group):
            if not fails(shown | {j}):
                shown.add(j)
    return build(shown)


def _sentence_spans(text):
    """(start, end) of each sentence of a field text; a citation token never ends or splits a sentence."""
    from finauditgate.application.research_narrative import _TOKEN
    from .thesis_repair import sentences
    spans, start, position = [], 0, 0
    for i, piece in enumerate(_TOKEN.split(text)):
        if i % 2:
            position += len(piece) + 4
            continue
        for part in sentences(piece):
            position += len(part)
            if _END.search(part):
                spans.append((start, position))
                start = position
    return spans + [(start, position)] if position > start else spans


def mask_numbers(report, draft, calculations, sources, request, *, changes, beliefs):
    """(masked report, [{field, original, masked, mode}]) or None when masking cannot make the report pass."""
    from finauditgate.application.research_delivery import DeliveryContext, report_context
    from finauditgate.application.research_narrative import _TOKEN
    from .thesis_repair import _set, final_texts

    def passes(value):
        try:
            report_context(value, draft, calculations, sources, request, changes=changes, beliefs=beliefs, contract=2)
        except ValueError as exc:
            if str(exc) != "UNBOUND_RESEARCH_NUMBER":
                raise
            return False
        return True

    try:
        if passes(report):
            return None  # nothing to hide
    except ValueError:
        return None  # refused for something other than numbers
    context = DeliveryContext(draft, calculations, sources, report, request, contract=2)

    def refused(stretch):
        try:
            context.unbound_numbers(stretch)
        except ValueError as exc:
            if str(exc) != "UNBOUND_RESEARCH_NUMBER":
                raise
            return True
        return False

    masked, rows = deepcopy(report), []
    try:
        for field, text in final_texts(report):
            pieces, marks, position = _TOKEN.split(text), [], 0
            for i in range(len(pieces)):
                if i % 2:
                    position += len(pieces[i]) + 4
                    continue
                if refused(pieces[i]):
                    found = _masked_stretch(pieces[i], refused)
                    if found is None:
                        return None
                    pieces[i] = found[0]
                    marks += [position + offset for offset in found[1]]
                position += len(pieces[i])
            done = "".join("{{" + p + "}}" if i % 2 else p for i, p in enumerate(pieces))
            if done == text:
                continue
            before, after = _sentence_spans(text), _sentence_spans(done)
            if len(before) != len(after):
                return None
            for (a, b), (c, d) in zip(before, after):
                if text[a:b] != done[c:d]:
                    rows.append({"field": field, "original": text[a:b], "masked": done[c:d],
                                 "mode": "sentence" if any(c <= m < d for m in marks) else "numbers"})
            _set(masked, field, done)
        if not rows or not passes(masked):
            return None
    except ValueError:
        return None
    return masked, rows
