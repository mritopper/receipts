"""Deterministic checks. No model is trusted to say "this quote is in the source" or "this number is in the quote".

- `locate_quote` finds a model-proposed quote in the real page text (exact after normalisation, then a strict
  fuzzy match, then ellipsis-split parts in order). Quotes that are not found are rejected as fabricated, and
  accepted quotes are replaced by the page's own wording, so what the reader sees is what the page says.
- `missing_numbers` lists figures in an answer sentence that appear in none of the quotes it cites.
- `fragment_url` builds a Text Fragment link (#:~:text=) that opens the source scrolled to, and highlighting,
  the quoted passage in Chromium browsers.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote as urlquote

_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
})


def _norm_char_map(text: str) -> tuple[str, list[int]]:
    """Normalised lowercase text plus, for each normalised char, its index in the original text."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        c = unicodedata.normalize("NFKC", ch).translate(_TRANS).lower()
        for cc in c:
            if cc.isspace() or cc in "*_#>|`":  # markdown decoration and whitespace collapse to one space
                if prev_space:
                    continue
                out.append(" ")
                idx.append(i)
                prev_space = True
            else:
                out.append(cc)
                idx.append(i)
                prev_space = False
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def normalise(text: str) -> str:
    return _norm_char_map(text)[0].strip()


@dataclass
class Match:
    start: int
    end: int
    method: str  # exact | fuzzy | ellipsis
    score: float
    text: str  # the page's own wording for the matched span


def locate_quote(quote: str, page: str, min_ratio: float = 0.9) -> Match | None:
    q = normalise(quote.strip().strip('"').strip("'"))
    if len(q) < 12 or not page:
        return None
    norm, idx = _norm_char_map(page)

    def span(a: int, b: int, method: str, score: float) -> Match:
        s, e = idx[a], idx[min(b, len(idx)) - 1] + 1
        shown = re.sub(r"\s+", " ", re.sub(r"[*_#`>|]+", "", page[s:e])).strip()
        return Match(s, e, method, round(score, 3), shown)

    pos = norm.find(q)
    if pos != -1:
        return span(pos, pos + len(q), "exact", 1.0)

    # "first part ... second part": every part must appear, in order, within a short window.
    parts = [p.strip(" .,;:") for p in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\])\s*", q) if len(p.strip(" .,;:")) >= 8]
    if len(parts) >= 2:
        start = norm.find(parts[0])
        cursor = start
        ok = start != -1
        for p in parts[1:]:
            if not ok:
                break
            nxt = norm.find(p, cursor + 1)
            ok = nxt != -1 and nxt - cursor < 1500
            cursor = nxt
        if ok:
            return span(start, cursor + len(parts[-1]), "ellipsis", 0.97)

    # Strict fuzzy match: anchor on the longest shared run, then score a same-length window around it.
    sm = difflib.SequenceMatcher(None, norm, q, autojunk=False)
    a, b, size = sm.find_longest_match(0, len(norm), 0, len(q))
    if size < max(12, len(q) // 4):
        return None
    best: tuple[float, int, int] = (0.0, 0, 0)
    base = a - b
    for shift in range(-12, 13, 3):
        s = max(0, base + shift)
        for extra in (-6, 0, 6):
            e = min(len(norm), s + len(q) + extra)
            r = difflib.SequenceMatcher(None, norm[s:e], q, autojunk=False).ratio()
            if r > best[0]:
                best = (r, s, e)
    if best[0] >= min_ratio:
        window = norm[best[1]:best[2]]
        # A near-match is only a match if it did not change a figure or flip a negation.
        if numbers_in(q) - numbers_in(window):
            return None
        if _negations(q) != _negations(window):
            return None
        return span(best[1], best[2], "fuzzy", best[0])
    return None


def _negations(t: str) -> int:
    return len(re.findall(r"\b(?:not|no|never|none|nor|without|cannot|n't)\b|n't", t))


_NUM = re.compile(r"(?<![\w.])[$€£]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])[$€£]?\d+(?:\.\d+)?%?")


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text):
        n = m.lstrip("$€£").rstrip("%").replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        if n and not (len(n) == 1 and n.isdigit()):  # single digits are too ambiguous to police
            out.add(n)
    return out


def missing_numbers(sentence: str, quotes: list[str]) -> list[str]:
    """Figures in the sentence that appear in none of its cited quotes (citation markers ignored)."""
    clean = re.sub(r"\[(?:E\d+(?:\s*,\s*)?)+\]", " ", sentence)
    have = set().union(*(numbers_in(q) for q in quotes)) if quotes else set()
    return sorted(n for n in numbers_in(clean) if n not in have)


def _enc(s: str) -> str:
    return urlquote(s, safe="").replace("-", "%2D")


def fragment_url(url: str, quote: str) -> str:
    """Link that opens `url` scrolled to and highlighting `quote` (Text Fragments; ignored by other browsers)."""
    words = [w for w in re.sub(r"\s+", " ", quote).strip().strip(".,;:!?\"'").split(" ") if w and w != "..."]
    if not words or not url:
        return url
    base = url.split("#", 1)[0]
    if len(words) <= 8:
        frag = _enc(" ".join(words))
    else:
        frag = f"{_enc(' '.join(words[:4]))},{_enc(' '.join(words[-4:]))}"
    return f"{base}#:~:text={frag}"
