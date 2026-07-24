#!/usr/bin/env python3
"""Stylistic feature extraction for the agent-written-code fingerprint.

Turns a block of source lines into a fixed-length numeric vector. Features are
deliberately *style* measures (comment habits, line shape, defensive-coding
density) rather than semantics, so the same vector is comparable across repos
and languages. Nothing here reads commit metadata: labels come from the caller.

Two design rules learned the hard way:
  - No single feature is treated as a verdict. The one high-precision tell we
    found (edit-elision comments) is just one dimension among ~40.
  - Every feature is a RATE, not a count, except log-size, so that hunks of
    different lengths are comparable.

`features(lines, lang)` -> dict; `vector(lines, lang)` -> list aligned to NAMES.
"""
from __future__ import annotations

import math
import re
from typing import Sequence

# --- language config ---------------------------------------------------------

HASH_LANGS = {"Python", "Ruby", "Shell", "YAML"}
CURLY_LANGS = {"TypeScript", "JavaScript", "Go", "Java", "C", "C++", "C#",
               "Rust", "Kotlin", "Swift", "Scala", "PHP", "Objective-C"}

EMOJI = re.compile("[\U0001F300-\U0001FAFF✅✨❌⭐⚠]")

# high-precision tell: an agent edited a file, elided the untouched region, and
# the elision comment got committed.
ELISION = re.compile(r"(?://|#)\s*\.\.\.\s*(existing|rest of|remaining|the rest|unchanged|other)",
                     re.I)
# phrasing that reads like narration to a reader rather than a note to self
EXPLANATORY = re.compile(
    r"\b(this (?:function|method|class|helper|module)|note that|we (?:need|want|must)|"
    r"in order to|for clarity|as (?:a|an) (?:result|example)|ensures? that|"
    r"handles? the case|this is (?:the|a)|which (?:means|allows))\b", re.I)
DIVIDER = re.compile(r"(?://|#)\s*[-=*_#]{4,}")
TODOISH = re.compile(r"\b(TODO|FIXME|XXX|HACK|NOTE):", re.I)
STEP_COMMENT = re.compile(r"(?://|#)\s*(?:step\s*\d|\d[.)])\s", re.I)

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
STRING_LIT = re.compile(r"\"([^\"\\\n]|\\.)*\"|'([^'\\\n]|\\.)*'")
NUMBER_LIT = re.compile(r"(?<![A-Za-z0-9_])\d+(\.\d+)?")
SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
CAMEL = re.compile(r"^[a-z][a-z0-9]*([A-Z][a-z0-9]*)+$")

GUARD = re.compile(r"\b(if\s*\(?\s*!|if\s+not\s|is None|== nil|!= nil|"
                   r"err\s*!=\s*nil|=== undefined|\?\?|catch\b|except\b|rescue\b)")
LOGGING = re.compile(r"\b(console\.(log|error|warn|info)|print\(|printf\(|"
                     r"logger?\.(debug|info|warn|warning|error)|log\.(Printf|Println|Fatal))")
RAISE = re.compile(r"\b(raise|throw)\s")
TYPEHINT = re.compile(r"(?:\)\s*(?:->|:)\s*[A-Za-z_\[\{]|"
                      r"[a-z_][A-Za-z0-9_]*\s*:\s*[A-Z][A-Za-z0-9_\[\]<>|.]*)")
FSTRING = re.compile(r"(?:f\"|f'|`[^`]*\$\{)")
KWARG = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^=]")
TERNARY = re.compile(r"\?[^?:\n]{1,40}:|(\bif\b.+\belse\b)")
AWAITY = re.compile(r"\b(await|async|\.then\(|go func|goroutine)\b")


def _comment_prefixes(lang: str) -> tuple[str, ...]:
    if lang in HASH_LANGS:
        return ("#",)
    if lang in CURLY_LANGS:
        return ("//", "/*", "*", "*/")
    return ("#", "//", "/*", "*")


def _safe(num: float, den: float) -> float:
    return num / den if den else 0.0


def _sd(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _split_comment(line: str, prefixes: tuple[str, ...]) -> tuple[str, str]:
    """Return (code_part, comment_part) — crude but language-agnostic."""
    stripped = line.lstrip()
    for p in prefixes:
        if stripped.startswith(p):
            return "", stripped[len(p):]
    best = len(line)
    for p in ("//", "#"):
        i = line.find(p)
        # skip the obvious false positive: a marker inside a string literal
        if i > 0 and line.count('"', 0, i) % 2 == 0 and line.count("'", 0, i) % 2 == 0:
            best = min(best, i)
    if best < len(line):
        return line[:best], line[best:].lstrip("/# ")
    return line, ""


def _comment_features(comments: list[str], n_code: int) -> dict[str, float]:
    n_c = len(comments)
    words = [len(c.split()) for c in comments]
    return {
        "comment_words_mean": _safe(sum(words), n_c),
        "comment_capital_rate": _safe(sum(1 for c in comments
                                          if c[:1].isupper()), n_c),
        "comment_period_rate": _safe(sum(1 for c in comments
                                         if c.rstrip().endswith(".")), n_c),
        "comment_long_rate": _safe(sum(1 for w in words if w >= 12), n_c),
        "comment_per_code": _safe(n_c, n_code),
        "explanatory_rate": _safe(sum(1 for c in comments
                                      if EXPLANATORY.search(c)), n_c),
        "step_comment_rate": _safe(sum(1 for c in comments
                                       if STEP_COMMENT.search("# " + c)), n_c),
        "todo_rate": _safe(sum(1 for c in comments if TODOISH.search(c)), n_c),
        "emoji_comment_rate": _safe(sum(1 for c in comments if EMOJI.search(c)), n_c),
    }


def _shape_features(lines: Sequence[str]) -> dict[str, float]:
    n = len(lines)
    lengths = [len(ln.rstrip()) for ln in lines]
    nonblank = [ln for ln in lines if ln.strip()]
    indents = [len(ln) - len(ln.lstrip()) for ln in nonblank]
    return {
        "log_lines": math.log1p(n),
        "line_len_mean": sum(lengths) / n,
        "line_len_sd": _sd(lengths),
        "line_len_p90": sorted(lengths)[int(0.9 * (n - 1))],
        "blank_rate": _safe(n - len(nonblank), n),
        "indent_mean": _safe(sum(indents), len(indents)),
        "indent_sd": _sd(indents),
        "tab_rate": _safe(sum(1 for ln in nonblank if ln.startswith("\t")), len(nonblank) or 1),
        "trailing_ws_rate": _safe(sum(1 for ln in nonblank
                                      if ln != ln.rstrip()), len(nonblank) or 1),
    }


def _code_features(code_lines: list[str], lang: str) -> dict[str, float]:
    n = len(code_lines) or 1
    body = "\n".join(code_lines)
    idents = IDENT.findall(body)
    strings = STRING_LIT.findall(body)
    str_texts = STRING_LIT.finditer(body)
    str_lens = [m.end() - m.start() - 2 for m in str_texts]
    return {
        "guard_rate": _safe(sum(1 for ln in code_lines if GUARD.search(ln)), n),
        "logging_rate": _safe(sum(1 for ln in code_lines if LOGGING.search(ln)), n),
        "raise_rate": _safe(sum(1 for ln in code_lines if RAISE.search(ln)), n),
        "typehint_rate": _safe(sum(1 for ln in code_lines if TYPEHINT.search(ln)), n),
        "fstring_rate": _safe(sum(1 for ln in code_lines if FSTRING.search(ln)), n),
        "kwarg_rate": _safe(sum(1 for ln in code_lines if KWARG.search(ln)), n),
        "ternary_rate": _safe(sum(1 for ln in code_lines if TERNARY.search(ln)), n),
        "async_rate": _safe(sum(1 for ln in code_lines if AWAITY.search(ln)), n),
        "semicolon_rate": _safe(sum(1 for ln in code_lines
                                    if ln.rstrip().endswith(";")), n),
        "brace_only_rate": _safe(sum(1 for ln in code_lines
                                     if ln.strip() in ("{", "}", "});", "};")), n),
        "ident_len_mean": _safe(sum(len(i) for i in idents), len(idents)),
        "ident_long_rate": _safe(sum(1 for i in idents if len(i) >= 14), len(idents) or 1),
        "snake_rate": _safe(sum(1 for i in idents if SNAKE.match(i)), len(idents) or 1),
        "camel_rate": _safe(sum(1 for i in idents if CAMEL.match(i)), len(idents) or 1),
        "string_rate": _safe(len(strings), n),
        "string_len_mean": _safe(sum(str_lens), len(str_lens)),
        "number_rate": _safe(len(NUMBER_LIT.findall(body)), n),
        "punct_rate": _safe(sum(1 for ch in body if ch in "(){}[],.;:="), max(len(body), 1)),
        "double_quote_pref": _safe(body.count('"'), body.count('"') + body.count("'") or 1),
    }


def features(lines: Sequence[str], lang: str) -> dict[str, float]:
    """Feature dict for one contiguous block of source lines."""
    if not lines:
        return {name: 0.0 for name in NAMES}
    prefixes = _comment_prefixes(lang)
    split = [_split_comment(ln, prefixes) for ln in lines]
    comments = [c.strip() for _, c in split if c.strip()]
    code_lines = [c for c, _ in split if c.strip()]
    full_comment = [ln for ln, (c, cm) in zip(lines, split) if not c.strip() and cm.strip()]
    body = "\n".join(lines)

    out = {
        **_shape_features(lines),
        **_comment_features(comments, len(code_lines) or 1),
        **_code_features(code_lines, lang),
        "comment_line_rate": _safe(len(full_comment), len(lines)),
        "inline_comment_rate": _safe(sum(1 for c, cm in split if c.strip() and cm.strip()),
                                     len(code_lines) or 1),
        "docstring_present": 1.0 if ('"""' in body or "'''" in body or "/**" in body) else 0.0,
        "divider_rate": _safe(sum(1 for ln in lines if DIVIDER.search(ln)), len(lines)),
        "elision_present": 1.0 if ELISION.search(body) else 0.0,
        "is_python": 1.0 if lang == "Python" else 0.0,
        "is_ts_js": 1.0 if lang in ("TypeScript", "JavaScript") else 0.0,
        "is_go": 1.0 if lang == "Go" else 0.0,
        "is_curly_other": 1.0 if lang in CURLY_LANGS - {"TypeScript", "JavaScript", "Go"} else 0.0,
    }
    return {name: float(out.get(name, 0.0)) for name in NAMES}


def vector(lines: Sequence[str], lang: str) -> list[float]:
    f = features(lines, lang)
    return [f[name] for name in NAMES]


# Canonical feature order. Derived once from a probe block so NAMES and the
# dicts above can never drift apart.
NAMES: tuple[str, ...] = (
    "log_lines", "line_len_mean", "line_len_sd", "line_len_p90", "blank_rate",
    "indent_mean", "indent_sd", "tab_rate", "trailing_ws_rate",
    "comment_line_rate", "inline_comment_rate", "comment_words_mean",
    "comment_capital_rate", "comment_period_rate", "comment_long_rate",
    "comment_per_code", "explanatory_rate", "step_comment_rate", "todo_rate",
    "emoji_comment_rate", "docstring_present", "divider_rate", "elision_present",
    "guard_rate", "logging_rate", "raise_rate", "typehint_rate", "fstring_rate",
    "kwarg_rate", "ternary_rate", "async_rate", "semicolon_rate",
    "brace_only_rate", "ident_len_mean", "ident_long_rate", "snake_rate",
    "camel_rate", "string_rate", "string_len_mean", "number_rate", "punct_rate",
    "double_quote_pref",
    "is_python", "is_ts_js", "is_go", "is_curly_other",
)

# Language indicators are controls, not style: the model keeps them so language
# mix can't masquerade as authorship, but they are excluded from any
# "what does the fingerprint look at" reporting.
CONTROL_NAMES = ("is_python", "is_ts_js", "is_go", "is_curly_other", "log_lines")
