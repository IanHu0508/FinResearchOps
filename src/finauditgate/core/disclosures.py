"""Rank source passages by financial topic, period and observed-vs-conditional wording.

The classes describe documentary support, not validated economic causation.
All quoted text and heading references are supplied by the filing reader.
"""

import re


_STOP = {"in", "of", "and", "the", "to", "from", "on", "net", "other", "change",
         "increase", "decrease", "non", "current", "asset", "liability", "by", "used"}
_CAUSAL = re.compile(r"\bbecause\b|\bdue to\b|\bdriven by\b|\battributable to\b|\bresulted from\b|\bas a result of\b", re.I)
_OBSERVED = re.compile(r"\b(?:increased|decreased|rose|fell|grew|declined|recognized|recorded|extended|was|were|reduced|made|paid|received|reported|amounted|fluctuated)\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:may|might|could|would|if|expect|expects|potential|risk|future)\b", re.I)
_POLICY = re.compile(r"accounted for|in accordance with|accounting polic|we evaluate|we initially record|we recognize|useful lives|method of accounting|topic \d{3}|accounting standards", re.I)
_PRIORITY = {"PERIOD_CHANGE_EXPLANATION": 0, "PERIOD_FACT": 1,
             "UNDATED_CHANGE_DESCRIPTION": 2, "RELATED_CONTEXT": 3,
             "OTHER_PERIOD": 4, "POLICY_BACKGROUND": 5, "RISK_BACKGROUND": 6}


def words(text):
    aliases = {"receivables": "receivable", "liabilities": "liability", "inventories": "inventory",
               "losses": "loss", "activities": "activity", "securities": "security"}
    result = []
    for word in re.findall(r"[a-z]{4,}", text.lower()):
        result.append(aliases.get(word, word[:-1] if word.endswith('s') and not word.endswith('ss') else word))
    return set(result)


def topic_match(label, text):
    terms = words(label) - _STOP
    actual = words(text)
    matched = terms & actual
    lower = text.lower()
    query = label.lower()
    if "operating cash" in query:
        good = bool(re.search(r"\b(?:operating cash flows?|cash (?:flows? )?(?:provided by|used in|from) operating activit(?:y|ies))\b", lower))
    elif "receivable" in query:
        good = "receivable" in actual
    elif "equity method" in query:
        good = bool(re.search(r"\bequity[- ]method\b|\bequity investees?\b", lower)) and bool(actual & {'income', 'earning', 'loss', 'gain', 'profit', 'impairment'})
    else:
        good = bool(matched) and len(matched) >= min(2, len(terms))
    return good, sorted(matched)


def classify(passage, current_year, label):
    text = passage["text"]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    observed = [s for s in sentences if topic_match(label, s)[0] and _OBSERVED.search(s)
                and not re.search(r"\b(?:if|may|might|could|would)\b.*\b(?:increase|decrease|decline|be|result)\b", s, re.I)]
    first = observed[0] if observed else text
    years = re.findall(r"\b(?:19|20)\d{2}\b", first)
    explicit = bool(years)
    if not years:
        years = re.findall(r"\b(?:19|20)\d{2}\b", passage.get("heading", ""))
    current = str(current_year) in years if current_year is not None else False
    causal = any(_CAUSAL.search(s) and not re.search(r"\b(?:if|may|might|could|would)\b", s, re.I) for s in sentences)
    if observed and current and causal:
        role = "PERIOD_CHANGE_EXPLANATION"
    elif observed and current:
        role = "PERIOD_FACT"
    elif _CONDITIONAL.search(text) and not observed:
        role = "RISK_BACKGROUND"
    elif _POLICY.search(text) and not (observed and causal):
        role = "POLICY_BACKGROUND"
    elif years and not current:
        role = "OTHER_PERIOD"
    elif observed and causal:
        role = "UNDATED_CHANGE_DESCRIPTION"
    else:
        role = "RELATED_CONTEXT"
    return {"evidence_role": role, "period_years": sorted(set(years)),
            "period_basis": "PASSAGE" if explicit else "HEADING" if years else "UNSPECIFIED"}


def rank(passages, label, current_year, limit):
    ranked = []
    seen = set()
    for passage in passages:
        match, terms = topic_match(label, passage["text"])
        if not match or passage["text"] in seen:
            continue
        seen.add(passage["text"])
        evidence = {**passage, **classify(passage, current_year, label), "matched_terms": terms}
        ranked.append(evidence)
    ranked.sort(key=lambda p: (_PRIORITY[p["evidence_role"]], -len(p["matched_terms"]), p["source"]["char_start"]))
    return ranked[:limit]
