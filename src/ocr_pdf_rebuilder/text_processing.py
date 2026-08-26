"""OCR text cleanup, markup normalization and formula preparation."""

from pathlib import Path
import hashlib
import html as html_lib
import re
import subprocess
import unicodedata

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def is_likely_math_text(text):
    text = str(text or "").strip()
    if not text or len(text) > 220:
        return False
    if re.search(r"\s{2,}", text):
        return False
    if re.fullmatch(r"[A-Za-zΑ-ω]", text):
        return True
    if re.search(r"[=+\-*/^_{}\\<>≤≥≈≠∈∉∂√∞∑∏∫]", text):
        return True
    if re.fullmatch(r"[A-Za-zΑ-ω0-9().,;: ]{1,80}", text) and re.search(r"[A-Za-zΑ-ω]", text):
        return True
    return False


def strip_known_inline_html(text):
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<(strong|b|em|i|code|mark|del|s|u|sup|sub)\b[^>]*>(.*?)</\1\s*>", r"\2", text)
    text = re.sub(r"(?is)</?(span|small|font)\b[^>]*>", "", text)
    return text


def normalize_inline_markup_aliases(text):
    text = str(text or "")
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<(strong|b)\b[^>]*>(.*?)</\1\s*>", r"**\2**", text)
    text = re.sub(r"(?is)<(em|i)\b[^>]*>(.*?)</\1\s*>", r"*\2*", text)
    text = re.sub(r"(?is)<code\b[^>]*>(.*?)</code\s*>", r"`\1`", text)
    text = re.sub(r"(?is)<(mark|u)\b[^>]*>(.*?)</\1\s*>", r"==\2==", text)
    text = re.sub(r"(?is)<(del|s)\b[^>]*>(.*?)</\1\s*>", r"~~\2~~", text)
    text = re.sub(r"(?is)<(sup|sub)\b[^>]*>(.*?)</\1\s*>", r"\2", text)
    text = re.sub(r"(?is)</?(span|small|font)\b[^>]*>", "", text)
    text = re.sub(r"\[\^([^\]\n]{1,24})\]", r"\1", text)
    text = re.sub(r"(?s)\\\((.{1,220}?)\\\)", r"$\1$", text)
    text = re.sub(r"(?s)\\\[(.{1,500}?)\\\]", r"$\1$", text)
    text = re.sub(r"(?<!\\)\$\$([^$\n]{1,500}?)(?<!\\)\$\$", r"$\1$", text)
    return text


def strip_dollar_math(text):
    def replace_display(match):
        return match.group(1).strip()

    def replace_inline(match):
        inner = match.group(1)
        return inner if is_likely_math_text(inner) else match.group(0)

    text = re.sub(r"(?<!\\)\$\$([^$\n]{1,500}?)(?<!\\)\$\$", replace_display, text)
    return re.sub(r"(?<!\\)\$([^$\n]{1,220}?)(?<!\\)\$", replace_inline, text)


def strip_markdown_inline(text):
    text = normalize_inline_markup_aliases(text)
    text = strip_known_inline_html(text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[\^([^\]\n]{1,24})\]", r"\1", text)
    text = re.sub(r"(?s)```[A-Za-z0-9_-]*\n?(.*?)```", r"\1", text)
    text = re.sub(r"(?m)^```[A-Za-z0-9_-]*\s*$", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(?s)\\\((.{1,220}?)\\\)", r"\1", text)
    text = re.sub(r"(?s)\\\[(.{1,500}?)\\\]", r"\1", text)
    text = strip_dollar_math(text)
    text = re.sub(r"\*\*\*([^*\n]+?)\*\*\*", r"\1", text)
    text = re.sub(r"___([^_\n]+?)___", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+?)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+?)~~", r"\1", text)
    text = re.sub(r"==([^=\n]+?)==", r"\1", text)
    text = re.sub(r"~([^~\n]{1,60}?)~", r"\1", text)
    text = re.sub(r"\^([^^\n]{1,60}?)\^", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", r"\1", text)
    return text


def strip_html_tags(text):
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def html_table_to_text(text):
    if not re.search(r"(?is)<\s*(table|tr|td|th)\b", text or ""):
        return text

    def convert_table(match):
        table = match.group(0)
        rows = []
        for row_match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>", table):
            row_html = row_match.group(1)
            cells = []
            for cell_match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row_html):
                cell = strip_html_tags(cell_match.group(1))
                cell = re.sub(r"[ \t\r\f\v]+", " ", cell)
                if cell:
                    cells.append(cell)
            if cells:
                rows.append("    ".join(cells))
        if rows:
            return "\n".join(rows)
        return strip_html_tags(table)

    text = re.sub(r"(?is)<table\b[^>]*>.*?</table\s*>", convert_table, text)
    text = re.sub(r"(?is)<tr\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)</tr\s*>", "\n", text)
    text = re.sub(r"(?is)</t[dh]\s*>\s*<t[dh]\b[^>]*>", " | ", text)
    text = re.sub(r"(?is)</?t[dh]\b[^>]*>", "", text)
    return strip_html_tags(text)


DOT_CHARS_CLASS = r"\.．·•∙‧⋅・･"
SPACED_DOT_LEADER_RE = re.compile(
    rf"(?:[ \t\r\n]*[{DOT_CHARS_CLASS}][ \t\r\n]*){{{DOT_LEADER_KEEP + 1},}}"
)
PLAIN_DOT_LEADER_RE = re.compile(rf"[{DOT_CHARS_CLASS}]{{{DOT_LEADER_KEEP + 1},}}")
DOT_LEADER_EXPLOSION_RE = re.compile(
    rf"(?:[ \t\r\n]*[{DOT_CHARS_CLASS}][ \t\r\n]*){{{DOT_LEADER_EXPLOSION_MIN_DOTS},}}"
)
DOT_LEADER_REPLACEMENT = " ".join(["."] * DOT_LEADER_KEEP)


def normalize_long_dot_leaders(text):
    # OCR models can hallucinate a single scanned dot leader into thousands of spaced dots.
    if text is None:
        return ""
    text = str(text)
    text = SPACED_DOT_LEADER_RE.sub(" " + DOT_LEADER_REPLACEMENT + " ", text)
    text = PLAIN_DOT_LEADER_RE.sub("." * DOT_LEADER_KEEP, text)
    return text


def repeated_text_key(text):
    text = normalize_unicode_preserving_greek_spacing(str(text or ""))
    text = strip_markdown_inline(text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"^[\W_]+|[\W_]+$", "", text, flags=re.UNICODE)
    return text


def collapse_consecutive_repeated_lines(text):
    lines = str(text or "").splitlines()
    if len(lines) < REPEATED_TEXT_MIN_RUN:
        return str(text or "")

    collapsed = []
    index = 0
    while index < len(lines):
        line = lines[index]
        key = repeated_text_key(line)
        if not key or len(line.strip()) > REPEATED_TEXT_MAX_UNIT_CHARS:
            collapsed.append(line)
            index += 1
            continue

        run_end = index + 1
        while run_end < len(lines) and repeated_text_key(lines[run_end]) == key:
            run_end += 1

        if run_end - index >= REPEATED_TEXT_MIN_RUN:
            collapsed.append(line)
        else:
            collapsed.extend(lines[index:run_end])
        index = run_end

    return "\n".join(collapsed)


def sentence_units_with_separators(text):
    parts = re.split(r"([^.!?。！？\n]{8,240}[.!?。！？]\s*)", str(text or ""))
    units = []
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[^.!?。！？\n]{8,240}[.!?。！？]\s*", part):
            units.append(("sentence", part))
        else:
            units.append(("other", part))

    return units


def collapse_consecutive_repeated_sentences(text):
    units = sentence_units_with_separators(text)
    if len(units) < REPEATED_TEXT_MIN_RUN:
        return text

    result = []
    index = 0
    while index < len(units):
        kind, value = units[index]
        if kind != "sentence":
            result.append(value)
            index += 1
            continue

        key = repeated_text_key(value)
        if not key or len(value.strip()) > REPEATED_TEXT_MAX_UNIT_CHARS:
            result.append(value)
            index += 1
            continue

        run_end = index + 1
        while run_end < len(units):
            next_kind, next_value = units[run_end]
            if next_kind != "sentence" or repeated_text_key(next_value) != key:
                break
            run_end += 1

        if run_end - index >= REPEATED_TEXT_MIN_RUN:
            result.append(value)
        else:
            result.extend(unit_value for _, unit_value in units[index:run_end])
        index = run_end

    return "".join(result)


def collapse_repeated_token_runs_in_line(line):
    if not line or len(line) < 24:
        return line

    tokens = re.findall(r"\S+", line)
    if len(tokens) < REPEATED_TEXT_MIN_RUN:
        return line

    collapsed = []
    index = 0
    max_ngram = min(REPEATED_TOKEN_MAX_NGRAM, len(tokens) // REPEATED_TEXT_MIN_RUN)
    while index < len(tokens):
        matched = False
        for ngram_size in range(1, max_ngram + 1):
            pattern = tokens[index:index + ngram_size]
            if len(pattern) < ngram_size:
                continue
            pattern_text = " ".join(pattern)
            if len(pattern_text) > REPEATED_TEXT_MAX_UNIT_CHARS:
                continue

            run_end = index + ngram_size
            run_count = 1
            while tokens[run_end:run_end + ngram_size] == pattern:
                run_count += 1
                run_end += ngram_size

            if run_count >= REPEATED_TEXT_MIN_RUN:
                collapsed.extend(pattern)
                index = run_end
                matched = True
                break

        if not matched:
            collapsed.append(tokens[index])
            index += 1

    collapsed_line = " ".join(collapsed)
    # Keep the original line when nothing meaningful changed so spacing-sensitive
    # prose is not normalized unnecessarily.
    if len(collapsed_line) >= len(line) * 0.96:
        return line
    return collapsed_line


def looks_like_repeated_token_artifact(text):
    tokens = re.findall(r"\S+", str(text or ""))
    if len(tokens) < 8:
        return False

    lowered = [token.lower() for token in tokens]
    counts = {}
    for token in lowered:
        counts[token] = counts.get(token, 0) + 1

    max_count = max(counts.values(), default=0)
    unique_ratio = len(counts) / max(len(lowered), 1)
    coordinate_count = sum(
        1
        for token in lowered
        if re.fullmatch(r"[xy]\s*=?-?\d+(?:\.\d+)?", token)
    )
    hash_marker_count = sum(1 for token in lowered if token.strip() == "###")

    if coordinate_count >= 4:
        return True
    if hash_marker_count >= 3:
        return True
    return max_count / max(len(lowered), 1) >= 0.18 or unique_ratio <= 0.38


def collapse_repeated_token_runs(text):
    lines = str(text or "").splitlines()
    if not lines:
        return str(text or "")
    collapsed = "\n".join(collapse_repeated_token_runs_in_line(line) for line in lines)
    if looks_like_repeated_token_artifact(collapsed):
        compacted = collapse_repeated_token_runs_in_line(re.sub(r"\s+", " ", collapsed).strip())
        if len(compacted) < len(collapsed) * 0.90:
            return compacted
    return collapsed


def collapse_repeated_ocr_text(text):
    text = collapse_repeated_token_runs(text)
    text = collapse_consecutive_repeated_lines(text)
    text = collapse_consecutive_repeated_sentences(text)
    return text



def has_dot_leader_explosion(text):
    return bool(DOT_LEADER_EXPLOSION_RE.search(str(text or "")))


def sanitize_layout_cell(cell):
    if not isinstance(cell, dict):
        return cell
    fixed = dict(cell)
    for key in ("text", "content", "html", "latex"):
        value = fixed.get(key)
        if value:
            fixed[key] = normalize_long_dot_leaders(str(value))
    return fixed


def sanitize_layout_cells(cells):
    return [sanitize_layout_cell(cell) for cell in cells]


def strip_control_chars(text):
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHAR_RE.sub("", text)
    return PDF_UNSAFE_CHAR_RE.sub("", text)


def normalize_fraction_markup(text):
    text = str(text or "")

    vulgar_fractions = {
        ("0", "3"): "↉",
        ("1", "2"): "½",
        ("1", "3"): "⅓",
        ("2", "3"): "⅔",
        ("1", "4"): "¼",
        ("3", "4"): "¾",
        ("1", "5"): "⅕",
        ("2", "5"): "⅖",
        ("3", "5"): "⅗",
        ("4", "5"): "⅘",
        ("1", "6"): "⅙",
        ("5", "6"): "⅚",
        ("1", "7"): "⅐",
        ("1", "8"): "⅛",
        ("3", "8"): "⅜",
        ("5", "8"): "⅝",
        ("7", "8"): "⅞",
        ("1", "9"): "⅑",

        ("1", "10"): "⅒",
    }

    def fraction_replacement(match):
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        return vulgar_fractions.get((numerator, denominator), f"{numerator}/{denominator}")

    def loose_fraction_replacement(match):
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        trailing_dot = "." if denominator.endswith(".") else ""
        denominator_key = denominator[:-1] if trailing_dot else denominator
        return vulgar_fractions.get(
            (numerator, denominator_key),
            f"{numerator}/{denominator_key}{trailing_dot}",
        )

    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\{\s*([0-9]+)\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\n\s*\{\s*([0-9]+\.?)\s*\}?",
        loose_fraction_replacement,
        text,
    )
    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\{\s*([0-9]+\.?)\s*\}?",
        loose_fraction_replacement,
        text,
    )
    text = re.sub(
        r"\^\s*\{\s*([0-9]+)\s*\}\s*/\s*_\s*\{\s*([0-9]+)\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\^\s*([0-9]+)\s*/\s*_\s*([0-9]+)",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\{\s*([0-9]{1,4})\s*/\s*([0-9]{1,4})\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"(?<![0-9])([0-9])\s*\u2044\s*([0-9]{1,2})(?![0-9])",
        fraction_replacement,
        text,
    )
    return text


def normalize_citation_superscripts(text):
    text = str(text or "")
    # MinerU may encode a numeric reference marker as Markdown/LaTeX superscript.
    # Keep the citation number while removing syntax that should not appear in prose.
    text = re.sub(r"\\textsuperscript\s*\{\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\}", r"\1", text)
    text = re.sub(r"\^\s*\{\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\}", r"\1", text)
    text = re.sub(r"\^\s*\(\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\)", r"\1", text)
    # OCR sometimes turns small inline text into LaTeX-style scripts, e.g.
    # "2lei]" -> "2^{lei}]". Keep the text and remove only the syntax.
    text = re.sub(r"([0-9A-Za-z])\^\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1\2", text)
    text = re.sub(r"([0-9A-Za-z])_\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1\2", text)
    text = re.sub(r"\^\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1", text)
    text = re.sub(r"_\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1", text)
    return text


GREEK_SPACING_MARKS = "\u1fbd\u1fbf\u1ffe\u1fef"
GREEK_SPACING_MARK_PLACEHOLDERS = {
    "\u1fbd": "\ue100",
    "\u1fbf": "\ue101",
    "\u1ffe": "\ue102",
    "\u1fef": "\ue103",
}
GREEK_SPACING_MARK_RESTORE = {
    placeholder: char for char, placeholder in GREEK_SPACING_MARK_PLACEHOLDERS.items()
}

VULGAR_FRACTION_CHARS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞↉"
VULGAR_FRACTION_PLACEHOLDERS = {
    char: chr(0xE110 + index) for index, char in enumerate(VULGAR_FRACTION_CHARS)
}
VULGAR_FRACTION_RESTORE = {
    placeholder: char for char, placeholder in VULGAR_FRACTION_PLACEHOLDERS.items()
}

SCRIPT_FORM_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
SCRIPT_FORM_PLACEHOLDERS = {
    char: chr(0xE140 + index) for index, char in enumerate(SCRIPT_FORM_CHARS)
}
SCRIPT_FORM_RESTORE = {
    placeholder: char for char, placeholder in SCRIPT_FORM_PLACEHOLDERS.items()
}


def normalize_unicode_preserving_greek_spacing(text):
    text = str(text or "")
    for char, placeholder in GREEK_SPACING_MARK_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    for char, placeholder in VULGAR_FRACTION_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    for char, placeholder in SCRIPT_FORM_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    text = unicodedata.normalize("NFKC", text)
    for placeholder, char in GREEK_SPACING_MARK_RESTORE.items():
        text = text.replace(placeholder, char)
    for placeholder, char in VULGAR_FRACTION_RESTORE.items():
        text = text.replace(placeholder, char)
    for placeholder, char in SCRIPT_FORM_RESTORE.items():
        text = text.replace(placeholder, char)
    return text


LATEX_SYMBOL_MAP = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ϱ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "ϕ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "neq": "≠",
    "ne": "≠",
    "approx": "≈",
    "sim": "∼",
    "simeq": "≃",
    "times": "×",
    "cdot": "·",
    "colon": ":",
    "pm": "±",
    "mp": "∓",
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "in": "∈",
    "notin": "∉",
    "subset": "⊂",
    "subseteq": "⊆",
    "supset": "⊃",

    "supseteq": "⊇",
    "cup": "∪",
    "cap": "∩",
    "to": "→",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "Leftarrow": "⇐",
    "leftrightarrow": "↔",
    "forall": "∀",
    "exists": "∃",
    "neg": "¬",
    "land": "∧",
    "lor": "∨",
    "parallel": "∥",
    "triangle": "△",
    "langle": "⟨",
    "rangle": "⟩",
    "cdots": "⋯",
    "ldots": "…",
    "dots": "…",
    "div": "÷",
    "checkmark": "✓",
    "cos": "cos",
    "sin": "sin",
    "tan": "tan",
    "log": "log",
    "ln": "ln",
    "textregistered": "®",
}

SUPERSCRIPT_MAP = str.maketrans({
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "n": "ⁿ",
    "i": "ⁱ",
})

SUBSCRIPT_MAP = str.maketrans({
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
})


def strip_latex_math_delimiters(text):
    text = str(text or "").strip()
    if text.startswith("$$") and text.endswith("$$") and len(text) >= 4:
        return text[2:-2].strip()
    pairs = [
        ("\\[", "\\]"),
        ("\\(", "\\)"),
        ("$", "$"),
    ]
    for start, end in pairs:
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            return text[len(start):-len(end)].strip()
    return text


def translate_script_text(value, mapping):
    translated = value.translate(mapping)
    if len(translated) != len(value):
        return None
    for source_char, target_char in zip(value, translated):
        if source_char == target_char and source_char.strip():
            return None
    return translated


def unwrap_redundant_latex_groups(value, max_depth=8):
    """Remove whole-value brace wrappers emitted redundantly by OCR models."""
    value = str(value or "").strip()
    for _ in range(max_depth):
        if not value.startswith("{"):
            break
        group = read_latex_group(value, 0)
        if group is None:
            break
        content, next_index, closed = group
        if not closed or next_index != len(value):
            break
        value = content.strip()
    return value


def collapse_nested_latex_script_groups(text):
    """Turn OCR forms such as ^{{3}} and _{{-1}} into ordinary groups."""
    text = str(text or "")
    atomic = r"[^{}\n]{1,24}"
    for _ in range(8):
        updated = re.sub(
            rf"([\^_])\s*\{{\s*\{{\s*({atomic})\s*\}}\s*\}}",
            r"\1{\2}",
            text,
        )
        if updated == text:
            break
        text = updated
    return text


def latex_scripts_to_unicode(text):
    text = collapse_nested_latex_script_groups(text)

    def replace_braced(match):
        base = match.group(1)
        marker = match.group(2)
        value = unwrap_redundant_latex_groups(match.group(3))
        value = re.sub(r"\s+", "", value)
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is None:
            return match.group(0)
        return base + translated

    def replace_single(match):
        base = match.group(1)
        marker = match.group(2)
        value = match.group(3)
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is None:
            return match.group(0)
        return base + translated

    def replace_unbraced_number(match):
        base = match.group(1)
        marker = match.group(2)
        value = re.sub(r"\s+", "", match.group(3))
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is None:
            return match.group(0)
        return base + translated

    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*([\^_])\s*\{\s*([^{}\n]{1,12})\s*\}",
        replace_braced,
        text,
    )

    def replace_standalone_braced(match):
        marker = match.group(1)
        value = unwrap_redundant_latex_groups(match.group(2))
        value = re.sub(r"\s+", "", value)
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is not None:
            return translated
        # PaddleOCR-VL emits the three-star editorial marker as the malformed
        # base-less expression ^{{**}}. Retaining the caret as a visible star
        # reconstructs the source marker instead of leaking TeX punctuation.
        if marker == "^" and re.fullmatch(r"\*{1,4}", value):
            return "*" + value
        return value

    text = re.sub(
        r"(?<![A-Za-zΑ-ω0-9\)\]])([\^_])\s*\{\s*([^{}\n]{1,24})\s*\}",
        replace_standalone_braced,
        text,
    )
    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*([\^_])\s*([+\-]?[0-9]{1,12})",
        replace_unbraced_number,
        text,
    )
    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*([\^_])\s*([0-9A-Za-z+\-=()])",
        replace_single,
        text,
    )
    return text


def replace_ocr_latex_hallucinations(text):
    """Repair high-confidence model encodings whose literal TeX is incorrect."""
    text = str(text or "")
    # In Greek scholarly entries Paddle substitutes commands for the omitted
    # initial alpha with psili/oxia. Keep this contextual so genuine mathring
    # and checkmark commands elsewhere retain their normal meaning.
    # The real Paddle payload can wrap only the hallucinated command in its own
    # math delimiters (``$ \\mathring{A} $τομοι``). Consume that wrapper and
    # the split word together so later dollar stripping cannot leave ``Å τομοι``
    # or ``✓ τομα`` behind.
    text = re.sub(
        r"\$\s*\\mathring\s*\{\s*A\s*\}\s*\$\s*τομοι\b",
        "Ἄτομοι",
        text,
    )
    text = re.sub(
        r"\$\s*\\checkmark\s*\$\s*τομα\b",
        "ἄτομα",
        text,
    )
    text = re.sub(r"\\mathring\s*\{\s*A\s*\}\s*τομοι\b", "Ἄτομοι", text)
    text = re.sub(r"\\checkmark\s*τομα\b", "ἄτομα", text)
    return text


def normalize_ocr_editorial_angle_markers(text):
    """Recover editorial angle brackets misread as command backslashes."""
    text = str(text or "")

    def replace_wrapped(match):
        word = match.group(1)
        if word in LATEX_FALLBACK_COMMANDS:
            return match.group(0)
        return f"⟨{word}⟩"

    text = re.sub(
        r"\\([A-Za-zÀ-ÖØ-öø-ÿ]{3,}(?:\[[^\]\n]{1,24}\])?)\\",
        replace_wrapped,
        text,
    )
    # A closing angle is occasionally omitted while the correction suffix is
    # retained, as in the real OCR form ``\\gegenübe[r]``.
    text = re.sub(
        r"\\([A-Za-zÀ-ÖØ-öø-ÿ]{3,}\[[^\]\n]{1,24}\])",
        lambda match: (
            match.group(0)
            if match.group(1).split("[", 1)[0] in LATEX_FALLBACK_COMMANDS
            else f"⟨{match.group(1)}⟩"
        ),
        text,
    )
    return text


def strip_suspicious_ocr_braces(text):
    """Remove nested or unmatched brace debris without touching balanced prose."""
    text = str(text or "")

    def grouped_number(match):
        base = match.group(1)
        value = re.sub(r"\s+", "", match.group(2))
        translated = translate_script_text(value, SUPERSCRIPT_MAP)
        return base + translated if translated is not None else match.group(0)

    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*\{\{\s*([+\-]?[0-9]{1,12})\s*\}\}",
        grouped_number,
        text,
    )
    # Only repeated braces are treated as OCR debris here. A normal balanced
    # single-brace expression remains available to legitimate prose/math.
    for pattern in (
        r"\{\{([^{}\n]{1,240})\}\}",
        r"\{\{([^{}\n]{1,240})\}",
        r"\{([^{}\n]{1,240})\}\}",
    ):
        for _ in range(4):
            updated = re.sub(pattern, r"\1", text)
            if updated == text:
                break
            text = updated
    text = re.sub(r"\{{2,}|\}{2,}", "", text)

    stack = []
    remove = set()
    for index, char in enumerate(text):
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            stack.append(index)
        elif char == "}" and (index == 0 or text[index - 1] != "\\"):
            if stack:
                stack.pop()
            else:
                remove.add(index)
    remove.update(stack)
    if remove:
        text = "".join(char for index, char in enumerate(text) if index not in remove)
    return text


def replace_latex_symbols(text):
    text = str(text or "")

    def replacement(match):
        command = match.group(1)
        return LATEX_SYMBOL_MAP.get(command, match.group(0))

    return re.sub(r"\\([A-Za-z]+)\b", replacement, text)


def unwrap_latex_text_commands(text):
    text = str(text or "")
    command = r"(?:text|mathrm|mathbf|mathit|mathsf|operatorname)"
    for _ in range(4):
        updated = re.sub(
            rf"\\{command}\s*\{{\s*([^{{}}\n]{{1,240}})\s*\}}",
            r"\1",
            text,
        )
        if updated == text:
            break
        text = updated
    return text


def latex_grouped_number_to_superscript(match):
    base = match.group(1)
    value = re.sub(r"\s+", "", match.group(2))
    translated = translate_script_text(value, SUPERSCRIPT_MAP)
    if translated is None:
        return match.group(0)
    return base + translated


def normalize_generic_latex_fractions(text):
    def replacement(match):
        numerator = re.sub(r"\s+", " ", match.group(1).strip())
        denominator = re.sub(r"\s+", " ", match.group(2).strip())
        return f"({numerator})/({denominator})"

    return re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([^{}\n]{1,100})\s*\}\s*\{\s*([^{}\n]{1,100})\s*\}",
        replacement,
        text,
    )



LATEX_FRACTION_COMMANDS = {"frac", "dfrac", "tfrac"}
LATEX_TEXT_COMMANDS = {"text", "mathrm", "mathbf", "mathit", "mathsf", "operatorname"}
LATEX_SCRIPT_COMMANDS = {"textsuperscript", "textsubscript"}
LATEX_ACCENT_COMMANDS = {
    "mathring": "\u030a",
    "ring": "\u030a",
    "breve": "\u0306",
    "vec": "\u20d7",
    "hat": "\u0302",
    "bar": "\u0304",
    "dot": "\u0307",
    "ddot": "\u0308",
    "tilde": "\u0303",
    "acute": "\u0301",
    "grave": "\u0300",
}
LATEX_LAYOUT_COMMANDS = {
    "left", "right", "bigl", "bigr", "Bigl", "Bigr", "big", "Big",
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
}
LATEX_SPACE_COMMANDS = {",", ";", ":", "!", "quad", "qquad", "enspace", "hspace", "vspace"}
LATEX_OPERATOR_MAP = {
    "sum": "∑",
    "prod": "∏",
    "int": "∫",
    "iint": "∬",
    "iiint": "∭",
    "lim": "lim",
}
LATEX_VULGAR_FRACTION_MAP = {
    ("0", "3"): "↉", ("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔",
    ("1", "4"): "¼", ("3", "4"): "¾", ("1", "5"): "⅕", ("2", "5"): "⅖",
    ("3", "5"): "⅗", ("4", "5"): "⅘", ("1", "6"): "⅙", ("5", "6"): "⅚",
    ("1", "7"): "⅐", ("1", "8"): "⅛", ("3", "8"): "⅜", ("5", "8"): "⅝",
    ("7", "8"): "⅞", ("1", "9"): "⅑", ("1", "10"): "⅒",
}
LATEX_FALLBACK_COMMANDS = (
    LATEX_FRACTION_COMMANDS
    | LATEX_TEXT_COMMANDS
    | LATEX_SCRIPT_COMMANDS
    | set(LATEX_ACCENT_COMMANDS)
    | LATEX_LAYOUT_COMMANDS
    | LATEX_SPACE_COMMANDS
    | set(LATEX_SYMBOL_MAP)
    | set(LATEX_OPERATOR_MAP)
    | {"sqrt", "begin", "end"}
)


def read_latex_group(text, start):
    """Return (content, next_index, closed) for a possibly truncated {...} group."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1, True
    return text[start + 1:], len(text), False


def skip_latex_space(text, start):
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def linearize_latex_commands(text, depth=0):
    """Linearize LaTeX commands without requiring balanced OCR output."""
    text = str(text or "")
    if depth > 24:
        return re.sub(r"\\[A-Za-z]+\*?", "", text).replace("{", "").replace("}", "")

    output = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            group = read_latex_group(text, index)
            if group is None:
                index += 1
                continue
            content, next_index, _closed = group
            output.append(linearize_latex_commands(content, depth + 1))
            index = next_index
            continue
        if char == "}":
            index += 1
            continue
        if char != "\\":
            output.append(char)
            index += 1
            continue

        if index + 1 >= len(text):
            index += 1
            continue
        escaped = text[index + 1]
        if escaped in "{}$%#&_":
            output.append(escaped)
            index += 2
            continue
        if escaped == "\\":
            output.append(" ")
            index += 2
            continue
        if escaped in LATEX_SPACE_COMMANDS:
            output.append(" ")
            index += 2
            continue

        command_match = re.match(r"[A-Za-z]+\*?", text[index + 1:])
        if not command_match:
            index += 2
            continue
        raw_command = command_match.group(0)
        command = raw_command.rstrip("*")
        next_index = index + 1 + len(raw_command)
        argument_index = skip_latex_space(text, next_index)

        if command == "n":
            # Some OCR JSON payloads contain a literal two-character ``\\n``
            # instead of an actual line break.
            output.append(" ")
            index = next_index
            continue

        if command in LATEX_FRACTION_COMMANDS:
            numerator_group = read_latex_group(text, argument_index)
            if numerator_group is None:
                index = next_index
                continue
            numerator, after_numerator, numerator_closed = numerator_group
            numerator_text = linearize_latex_commands(numerator, depth + 1).strip()
            denominator_index = skip_latex_space(text, after_numerator)
            denominator_group = read_latex_group(text, denominator_index) if numerator_closed else None
            if denominator_group is None:
                # Truncated OCR such as "\\frac{400 - ..." has no denominator.
                # Preserve the recoverable payload, but never leak the command.
                output.append(numerator_text)
                index = after_numerator
                continue
            denominator, after_denominator, _denominator_closed = denominator_group
            denominator_text = linearize_latex_commands(denominator, depth + 1).strip()
            if numerator_text and denominator_text:
                output.append(
                    LATEX_VULGAR_FRACTION_MAP.get(
                        (numerator_text, denominator_text),
                        f"({numerator_text})/({denominator_text})",
                    )
                )
            else:
                output.append(numerator_text or denominator_text)
            index = after_denominator
            continue

        if command == "sqrt":
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                output.append(f"√({linearize_latex_commands(content, depth + 1).strip()})")
                index = after_group
                continue

        if command in LATEX_TEXT_COMMANDS:
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                output.append(linearize_latex_commands(content, depth + 1))
                index = after_group
                continue

        if command in LATEX_SCRIPT_COMMANDS:
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                content = linearize_latex_commands(content, depth + 1).strip()
                mapping = SUPERSCRIPT_MAP if command == "textsuperscript" else SUBSCRIPT_MAP
                translated = translate_script_text(re.sub(r"\s+", "", content), mapping)
                output.append(translated if translated is not None else content)
                index = after_group
                continue
            existing_script = re.match(
                rf"[{re.escape(SCRIPT_FORM_CHARS)}]+",
                text[argument_index:],
            )
            if existing_script:
                output.append(existing_script.group(0))
                index = argument_index + len(existing_script.group(0))
                continue

        if command in LATEX_ACCENT_COMMANDS:
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                content = linearize_latex_commands(content, depth + 1).strip()
                output.append(
                    unicodedata.normalize("NFC", content + LATEX_ACCENT_COMMANDS[command])
                )
                index = after_group
                continue

        if command in {"begin", "end"}:
            group = read_latex_group(text, argument_index)
            index = group[1] if group is not None else next_index
            output.append(" ")
            continue
        if command in LATEX_SYMBOL_MAP:
            output.append(LATEX_SYMBOL_MAP[command])
        elif command in LATEX_OPERATOR_MAP:
            output.append(LATEX_OPERATOR_MAP[command])
        elif command in LATEX_SPACE_COMMANDS:
            output.append(" ")
        elif command not in LATEX_LAYOUT_COMMANDS:
            # A true unknown command with a braced payload is most likely a
            # style macro, so keep its payload but discard the macro name. An
            # unbraced unknown is often OCR prose misread as a command; retain
            # the word so normalization never silently deletes source text.
            if argument_index >= len(text) or text[argument_index] != "{":
                output.append(command)
        index = next_index

    return "".join(output)


def strip_paired_latex_dollar_delimiters(text):
    text = str(text or "")

    def replacement(match):
        content = match.group(1) or match.group(2) or ""
        looks_like_math = bool(
            "\\" in content
            or re.search(r"[_^=+*/<>≤≥≠≈∑∏∫√]", content)

            or re.fullmatch(r"\s*[A-Za-zΑ-ω]\s*", content)
        )
        return content if looks_like_math else match.group(0)

    for _ in range(4):
        updated = re.sub(r"\$\$([^$\n]+?)\$\$|\$([^$\n]+?)\$", replacement, text)
        if updated == text:
            break
        text = updated
    return text


def contains_latex_fallback_command(text):
    return bool(re.search(r"\\(?:[A-Za-z]+\*?|[,;:!])", str(text or "")))


def normalize_safe_latex_markup(text):
    text = str(text or "")
    text = replace_ocr_latex_hallucinations(text)
    text = normalize_ocr_editorial_angle_markers(text)
    should_linearize_commands = contains_latex_fallback_command(text)
    text = strip_paired_latex_dollar_delimiters(text)
    text = text.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    text = latex_scripts_to_unicode(text)
    # Parse the complete command stream before unwrapping style commands or
    # applying the generic word{number}->superscript heuristic. If a style
    # command is removed first, adjacent input such as
    # ``\\cdot\\operatorname{Cos.}`` collapses to ``\\cdotCos.`` and the
    # parser can no longer distinguish the symbol command from its payload.
    if should_linearize_commands:
        text = linearize_latex_commands(text)
    text = unwrap_latex_text_commands(text)
    text = re.sub(
        r"([A-Za-zΑ-ω]{2,})\s*\{\s*([0-9]{1,3})\s*\}",
        latex_grouped_number_to_superscript,
        text,
    )
    text = replace_latex_symbols(text)
    text = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big|displaystyle|textstyle)\b", "", text)
    text = normalize_fraction_markup(text)
    text = latex_scripts_to_unicode(text)
    text = strip_suspicious_ocr_braces(text)
    text = re.sub(r"⟨\s+", "⟨", text)
    text = re.sub(r"\s+⟩", "⟩", text)
    return text


def clean_latex_source(text):
    text = html_lib.unescape(str(text or ""))
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return strip_latex_math_delimiters(text)


def linearize_latex_formula(text):
    text = clean_latex_source(text)
    text = strip_paired_latex_dollar_delimiters(text)
    text = normalize_safe_latex_markup(text)
    text = linearize_latex_commands(text)
    text = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big|displaystyle|textstyle)\b", "", text)
    text = re.sub(r"\\(?:mathrm|mathbf|mathit|mathsf|text|operatorname)\s*\{\s*([^{}]{1,160})\s*\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    # A script can become adjacent to its base only after the surrounding
    # braces have been removed (for example S_{F}^{-1} -> S_F^-1). Run the
    # conversion once more so signed numeric scripts are rendered as real
    # Unicode superscripts instead of leaking raw markup into font routing.
    text = latex_scripts_to_unicode(text)
    text = text.replace("$", "")
    text = text.replace("~", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = normalize_unicode_preserving_greek_spacing(text)
    return strip_control_chars(text).strip()


def formula_source_text(block):
    return str(block.get("source_text") or block.get("text") or "")


FORMULA_STRUCTURAL_COMMAND_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|iiint|lim|begin|matrix|cases)\b"
)
FORMULA_OPERATOR_RE = re.compile(r"[=+*/<>≤≥≈≠∈∉∂√∞∑∏∫]|(?<!\w)-(?!\w)")
LATEX_CITATION_ONLY_RE = re.compile(
    r"^(?:\$\$?|\\\(|\\\[)?\s*\^\s*\{\s*[^{}]{1,8}\s*\}\s*(?:\$\$?|\\\)|\\\])?$"
)


def fully_delimited_math_body(text):
    text = str(text or "").strip()
    for start, end in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            return text[len(start):-len(end)].strip()
    return None


def formula_body_has_substantive_math(text):
    text = str(text or "").strip()
    if not text or LATEX_CITATION_ONLY_RE.fullmatch(text):
        return False
    if FORMULA_STRUCTURAL_COMMAND_RE.search(text):
        return True
    return bool(FORMULA_OPERATOR_RE.search(text) and re.search(r"[0-9A-Za-zΑ-ω]", text))


def is_formula_render_block(block):
    category = block.get("category")
    raw = formula_source_text(block).strip()
    if category == "Formula":
        return True
    if not raw or len(raw) > 180:
        return False
    if re.search(r"[\u4e00-\u9fff].{20,}", raw):
        return False

    delimited_body = fully_delimited_math_body(raw)
    if delimited_body is not None:
        return formula_body_has_substantive_math(delimited_body)

    # Recover a genuinely formula-like block that lost its outer delimiters,
    # but never promote prose merely because it contains an inline citation,
    # inline variable, or superscript edition marker.
    if not FORMULA_STRUCTURAL_COMMAND_RE.search(raw):
        return False
    prose_without_commands = re.sub(r"\\[A-Za-z]+\*?", " ", raw)
    prose_words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", prose_without_commands)
    return len(prose_words) <= 1


def formula_to_unicode_if_simple(text):
    source = clean_latex_source(text)
    if not source or len(source) > 90:
        return None
    complex_commands = (
        "\\begin",
        "\\sum",
        "\\prod",
        "\\int",
        "\\lim",
        "\\matrix",
        "\\cases",
        "\\over",
        "\\underset",
        "\\overset",
    )
    if any(command in source for command in complex_commands):
        return None
    script_source = collapse_nested_latex_script_groups(source)
    for match in re.finditer(
        r"([\^_])\s*\{\s*([^{}\n]{1,24})\s*\}",
        script_source,
    ):
        marker, value = match.groups()
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        if translate_script_text(re.sub(r"\s+", "", value), mapping) is None:
            # Preserve complex subscripts such as k_{\mu} and S_{F} for the
            # vector formula renderer. Plain-text hardening must not make a
            # lossy linearization look deceptively complete.
            return None
    simplified = linearize_latex_formula(source)
    if not simplified:
        return None
    # Residual script markers mean the Unicode conversion was not lossless
    # (for example an uppercase subscript such as S_{F}). Let the vector
    # formula renderer preserve the notation instead of feeding those markers
    # to the Markdown inline parser.
    if re.search(r"[\\{}_^]", simplified):
        return None
    if len(simplified) > 110:
        return None
    return simplified


def latex_formula_document(formula):
    formula = clean_latex_source(formula)
    return "\n".join(
        [
            r"\documentclass[12pt]{article}",
            r"\usepackage[utf8]{inputenc}",
            r"\usepackage{amsmath,amssymb,bm}",
            r"\pagestyle{empty}",
            r"\begin{document}",
            r"\[",
            formula,
            r"\]",
            r"\end{document}",
            "",
        ]
    )


def render_latex_formula_to_svg(formula, cache_dir):
    deps = formula_render_dependencies()
    if not deps.get("latex") or not deps.get("dvisvgm") or not deps.get("svglib"):
        raise RuntimeError("formula vector dependencies missing")

    formula = clean_latex_source(formula)
    if not formula:
        raise RuntimeError("empty formula")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(formula.encode("utf-8", errors="replace")).hexdigest()

    case_dir = cache_dir / digest
    svg_path = case_dir / "formula.svg"
    if svg_path.exists() and svg_path.stat().st_size > 0:
        return svg_path

    case_dir.mkdir(parents=True, exist_ok=True)
    tex_path = case_dir / "formula.tex"
    tex_path.write_text(latex_formula_document(formula), encoding="utf-8")

    latex_engine = deps["latex"]
    latex_cmd = [
        latex_engine,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "formula.tex",
    ]
    latex_result = subprocess.run(
        latex_cmd,
        cwd=str(case_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
        check=False,
    )
    if latex_result.returncode != 0:
        tail = "\n".join((latex_result.stdout or "").splitlines()[-10:])
        raise RuntimeError(f"latex failed: {tail}")

    engine_name = Path(latex_engine).name.lower()
    if "pdflatex" in engine_name:
        formula_output = case_dir / "formula.pdf"
        dvisvgm_input_args = ["--pdf", str(formula_output)]
        if not formula_output.exists():
            raise RuntimeError("pdflatex did not create formula.pdf")
    else:
        formula_output = case_dir / "formula.dvi"
        dvisvgm_input_args = [str(formula_output)]
        if not formula_output.exists():
            raise RuntimeError("latex did not create formula.dvi")

    dvisvgm_cmd = [
        deps["dvisvgm"],
        "--no-fonts",
        "--exact",
        "--bbox=min",
        "-o",
        str(svg_path),
        *dvisvgm_input_args,
    ]
    dvisvgm_result = subprocess.run(
        dvisvgm_cmd,
        cwd=str(case_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
        check=False,
    )
    if dvisvgm_result.returncode != 0 or not svg_path.exists():
        tail = "\n".join((dvisvgm_result.stdout or "").splitlines()[-10:])
        raise RuntimeError(f"dvisvgm failed: {tail}")
    return svg_path


def draw_svg_in_rect(c, page_height, rect, svg_path):
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg

    drawing = svg2rlg(str(svg_path))
    if drawing is None:
        return False
    width = float(getattr(drawing, "width", 0.0) or 0.0)
    height = float(getattr(drawing, "height", 0.0) or 0.0)
    if width <= 0 or height <= 0:
        return False

    scale = min(rect.width / width, rect.height / height)
    if scale <= 0:
        return False
    x = rect.x0 + (rect.width - width * scale) / 2
    y = page_height - rect.y1 + (rect.height - height * scale) / 2
    c.saveState()
    drawing.scale(scale, scale)
    renderPDF.draw(drawing, c, x, y)
    c.restoreState()
    return True


def reportlab_draw_hidden_plain_text(c, page_height, rect, text, fontsize=2.0):
    text = normalize_markdown_text(linearize_latex_formula(text))
    if not text:
        return
    fontsize = max(1.0, min(float(fontsize), max(1.0, rect.height * 0.45)))
    baseline = page_height - rect.y0 - fontsize
    cursor = rect.x0
    # PDF text rendering mode persists across text objects. Isolate mode 3
    # (invisible text) so formula search text cannot make a following equation
    # number, footer, or page number invisible.
    c.saveState()
    try:
        for seg in reportlab_plain_segments(text):
            seg_text = seg.get("text", "")
            if not seg_text:
                continue
            fontname, _fake_bold = reportlab_font_for_segment(seg, "normal")
            text_obj = c.beginText(cursor, baseline)
            text_obj.setFont(fontname, fontsize)
            text_obj.setTextRenderMode(3)
            text_obj.textOut(text_for_single_pdf_draw(seg_text))
            c.drawText(text_obj)
            cursor += reportlab_text_width(seg_text, fontname, fontsize)
    finally:
        c.restoreState()


def render_pdf_rect_to_png(pdf_path, page_index, rect, image_path, padding=FORMULA_CROP_PADDING, dpi=FORMULA_RENDER_DPI):
    image_path = Path(image_path)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise RuntimeError(f"page index out of range: {page_index}")
        page = doc[page_index]
        clip = fitz.Rect(rect)
        clip.x0 = max(page.rect.x0, clip.x0 - padding)
        clip.y0 = max(page.rect.y0, clip.y0 - padding)
        clip.x1 = min(page.rect.x1, clip.x1 + padding)
        clip.y1 = min(page.rect.y1, clip.y1 + padding)
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        pix.save(str(image_path))
    return image_path


def draw_formula_crop_fallback(c, page_height, rect, block):
    source_pdf = block.get("source_pdf_path")
    page_index = block.get("page_index")
    crop_dir = block.get("formula_crop_dir")
    if source_pdf is None or page_index is None or crop_dir is None:
        return False
    crop_path = Path(crop_dir) / f"page_{int(page_index) + 1:04d}_formula_{block.get('order', 0)}.png"
    render_pdf_rect_to_png(source_pdf, int(page_index), rect, crop_path)
    c.drawImage(
        str(crop_path),
        rect.x0,
        page_height - rect.y1,
        width=rect.width,
        height=rect.height,
        preserveAspectRatio=True,
        anchor="c",
        mask="auto",
    )
    reportlab_draw_hidden_plain_text(c, page_height, rect, formula_source_text(block))
    return True


def render_formula_block(c, page_height, rect, block, formula_work_dir, allow_image_fallback=True):
    source = formula_source_text(block)
    simple = formula_to_unicode_if_simple(source)
    has_unsupported_vector_combining_mark = bool(simple and "\u20d7" in simple)
    if simple and not has_unsupported_vector_combining_mark:
        fontsize = float(block.get("font_size", max(5.0, rect.height * 0.55)))
        min_size = float(block.get("min_font_size", max(3.2, fontsize * 0.72)))
        line_height = float(block.get("line_height", 1.0))
        fontsize, line_height, ok = reportlab_fit_box_params(
            page_height,
            rect,
            simple,
            fontsize,
            min_size,
            line_height,
            block_align(block),
        )
        if ok and reportlab_draw_markdown_textbox(c, page_height, rect, simple, fontsize, line_height, block_align(block)):
            block["formula_render_mode"] = "unicode_text"
            return True

    # U+20D7 (COMBINING RIGHT ARROW ABOVE) is not available in the bundled
    # text fonts and therefore rendered as a visible tofu square.  It is also
    # a common PaddleOCR confusion for a scanned superscript.  The image
    # variant can preserve the exact source crop; the text-only variant uses
    # an explicit vec(...) spelling instead of emitting a missing glyph.
    if has_unsupported_vector_combining_mark and allow_image_fallback:
        try:
            if draw_formula_crop_fallback(c, page_height, rect, block):
                block["formula_render_mode"] = "image_crop"
                block["formula_crop_reason"] = "unsupported_combining_vector_mark"
                return True
        except Exception as exc:
            block["formula_render_error"] = str(exc)

    if not has_unsupported_vector_combining_mark:
        cache_dir = block.get("formula_cache_dir") or formula_work_dir
        try:
            svg_path = render_latex_formula_to_svg(source, cache_dir)
            if draw_svg_in_rect(c, page_height, rect, svg_path):
                reportlab_draw_hidden_plain_text(c, page_height, rect, source)
                block["formula_render_mode"] = "latex_svg"
                return True
        except Exception as exc:
            block["formula_render_error"] = str(exc)

    if has_unsupported_vector_combining_mark:
        safe_source = re.sub(r"\\vec\s*\{\s*([^{}]+?)\s*\}", r"vec(\1)", source)
        linear = linearize_latex_formula(safe_source)
    else:
        linear = linearize_latex_formula(source)

    if linear:
        fontsize = float(block.get("font_size", max(5.0, rect.height * 0.55)))
        min_size = float(block.get("min_font_size", max(3.2, fontsize * 0.65)))
        line_height = float(block.get("line_height", 1.0))
        fontsize, line_height, ok = reportlab_fit_box_params(
            page_height,
            rect,
            linear,
            fontsize,
            min_size,
            line_height,
            block_align(block),
        )
        if ok and reportlab_draw_markdown_textbox(c, page_height, rect, linear, fontsize, line_height, block_align(block)):
            block["formula_render_mode"] = "linear_text"
            return True

    if allow_image_fallback:
        try:
            if draw_formula_crop_fallback(c, page_height, rect, block):
                block["formula_render_mode"] = "image_crop"
                return True
        except Exception as exc:
            previous = block.get("formula_render_error")
            block["formula_render_error"] = f"{previous}; crop fallback failed: {exc}" if previous else str(exc)
    return False


def text_for_single_pdf_draw(text):
    return strip_control_chars(text).replace("\n", " ")


def normalize_ocr_symbol_confusables(text):
    text = str(text or "")
    # PaddleOCR-VL can confuse an italic capital psi with U+26B2 NEUTER in
    # scanned physics prose. Restrict the correction to the lexical context
    # used for a wave function so a genuine symbol elsewhere is preserved for
    # the renderer's generic missing-glyph policy.
    return re.sub(r"⚲(?=\s*函数)", "Ψ", text)


def normalize_ocr_markup_text(text):
    text = html_table_to_text(text)
    text = html_lib.unescape(text)
    text = normalize_ocr_symbol_confusables(text)
    text = normalize_safe_latex_markup(text)
    text = normalize_citation_superscripts(text)
    text = normalize_safe_latex_markup(text)
    text = strip_control_chars(text)
    text = normalize_long_dot_leaders(text)
    return collapse_repeated_ocr_text(text)


def markdown_table_cells(line):
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped):
        return []
    if stripped.startswith("|") or stripped.endswith("|") or stripped.count("|") >= 1:
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            return cells
    return None


def source_text_from_cell(cell):
    for key in ("text", "content", "html", "latex"):
        value = cell.get(key)
        if value:
            return str(value)
    return ""


def extract_html_table_rows(text):
    if not re.search(r"(?is)<\s*(table|tr|td|th)\b", text or ""):
        return []

    rows = []
    for row_match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>", text):
        row_html = row_match.group(1)
        cells = []
        for cell_match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row_html):
            cell = strip_html_tags(cell_match.group(1))
            cell = re.sub(r"[ \t\r\f\v]+", " ", cell).strip()
            cells.append(normalize_text(cell) if cell else "")
        if not cells:
            continue
        nonempty = [cell for cell in cells if cell]
        if not nonempty:
            continue
        first_col = next((idx for idx, cell in enumerate(cells) if cell), 0)
        rows.append(
            {
                "cells": cells,
                "nonempty_cells": nonempty,
                "first_col": first_col,
                "col_count": len(cells),
                "source": "html",
            }
        )
    return rows


def extract_plain_table_rows(text):
    text = normalize_markdown_text(text)
    rows = []
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line:
            continue
        cells = markdown_table_cells(line)
        if cells == []:
            continue
        if cells is None:
            cells = [line]
        cells = [normalize_text(cell) for cell in cells]
        nonempty = [cell for cell in cells if cell]
        if not nonempty:
            continue
        first_col = next((idx for idx, cell in enumerate(cells) if cell), 0)
        rows.append(
            {
                "cells": cells,
                "nonempty_cells": nonempty,
                "first_col": first_col,
                "col_count": len(cells),
                "source": "plain",
            }
        )
    return rows


def extract_table_rows(text):
    return extract_html_table_rows(text) or extract_plain_table_rows(text)


def table_rows_from_text(text):
    return [row["nonempty_cells"] for row in extract_table_rows(text)]


def table_rows_for_block(block_or_text):
    if isinstance(block_or_text, dict):
        rows = block_or_text.get("table_rows") or extract_table_rows(block_or_text.get("source_text") or block_or_text.get("text", ""))
        return rows
    return extract_table_rows(block_or_text)


def table_row_parts(row):
    cells = row.get("nonempty_cells", row) if isinstance(row, dict) else row
    if len(cells) >= 2:
        last = cells[-1].strip()
        if re.fullmatch(r"\[?\d{1,4}\]?|[ivxlcdmIVXLCDM]{1,8}", last) or len(last) <= 8:
            return "  ".join(cells[:-1]).strip(), last
    return " | ".join(cells).strip(), ""


def table_row_level(row, left_text):
    if isinstance(row, dict) and row.get("source") == "html" and row.get("first_col", 0) > 0:
        return min(3, int(row["first_col"]))

    text = left_text.strip()
    if re.match(r"^[a-z]\.", text):
        return 2
    if re.match(r"^(?:[IVX]+)\.", text):
        return 1
    if re.match(r"^[A-Z]\.", text):
        return 0
    if re.match(r"^第[一二三四五六七八九十百0-9]+[章节部分]", text):
        return 0
    return 0


def is_page_number_like(text):
    return bool(re.fullmatch(r"\[?\d{1,4}\]?|[ivxlcdmIVXLCDM]{1,8}", str(text or "").strip()))


def is_toc_table(rows):
    if len(rows) < 2:
        return False
    page_like = 0
    usable = 0
    for row in rows:
        cells = row.get("nonempty_cells", [])
        if len(cells) < 2:
            continue
        usable += 1
        if is_page_number_like(cells[-1]):
            page_like += 1

    return usable > 0 and page_like / usable >= 0.60


def normalize_markdown_text(text):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return strip_control_chars(text).strip()


def normalize_text(text):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}$", "", text)
    text = strip_markdown_inline(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return strip_control_chars(text).strip()


def is_font_test_text(text):
    return bool(FONT_TEST_TEXT_RE.fullmatch(normalize_text(text or "")))


def normalize_draw_segment_text(text, strip_markdown=True):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    if strip_markdown:
        text = strip_markdown_inline(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    if LATEX_RESIDUE_RE.search(text):
        text = normalize_ocr_markup_text(linearize_latex_formula(text))
    if LATEX_RESIDUE_RE.search(text):
        raise RuntimeError(f"LaTeX residue remains at PDF draw boundary: {text[:160]!r}")
    return strip_control_chars(text)


def assert_no_radicals_in_text(text, context):
    chars = sorted(set(RADICAL_CHAR_RE.findall(text or "")))
    if chars:
        details = " ".join(f"{ch}(U+{ord(ch):04X})" for ch in chars)
        raise RuntimeError(f"Radical characters remain before PDF rendering in {context}: {details}")


def normalize_category(category):
    category = str(category or "Text").strip()
    return category or "Text"


def get_text_from_cell(cell):
    return normalize_markdown_text(source_text_from_cell(cell))


_COMPONENT_EXPORTS = (
    "is_likely_math_text",
    "strip_known_inline_html",
    "normalize_inline_markup_aliases",
    "strip_dollar_math",
    "strip_markdown_inline",
    "strip_html_tags",
    "html_table_to_text",
    "normalize_long_dot_leaders",
    "repeated_text_key",
    "collapse_consecutive_repeated_lines",
    "sentence_units_with_separators",
    "collapse_consecutive_repeated_sentences",
    "collapse_repeated_token_runs_in_line",
    "looks_like_repeated_token_artifact",
    "collapse_repeated_token_runs",
    "collapse_repeated_ocr_text",
    "has_dot_leader_explosion",
    "sanitize_layout_cell",
    "sanitize_layout_cells",
    "strip_control_chars",
    "normalize_fraction_markup",
    "normalize_citation_superscripts",
    "normalize_unicode_preserving_greek_spacing",
    "strip_latex_math_delimiters",
    "translate_script_text",
    "latex_scripts_to_unicode",
    "replace_latex_symbols",
    "unwrap_latex_text_commands",
    "latex_grouped_number_to_superscript",
    "normalize_generic_latex_fractions",
    "read_latex_group",
    "skip_latex_space",
    "linearize_latex_commands",
    "strip_paired_latex_dollar_delimiters",
    "contains_latex_fallback_command",
    "normalize_safe_latex_markup",
    "clean_latex_source",
    "linearize_latex_formula",
    "formula_source_text",
    "fully_delimited_math_body",
    "formula_body_has_substantive_math",
    "is_formula_render_block",
    "formula_to_unicode_if_simple",
    "latex_formula_document",
    "render_latex_formula_to_svg",
    "draw_svg_in_rect",
    "reportlab_draw_hidden_plain_text",
    "render_pdf_rect_to_png",
    "draw_formula_crop_fallback",
    "render_formula_block",
    "text_for_single_pdf_draw",
    "normalize_ocr_symbol_confusables",
    "normalize_ocr_markup_text",
    "markdown_table_cells",
    "source_text_from_cell",
    "extract_html_table_rows",
    "extract_plain_table_rows",
    "extract_table_rows",
    "table_rows_from_text",
    "table_rows_for_block",
    "table_row_parts",
    "table_row_level",
    "is_page_number_like",
    "is_toc_table",
    "normalize_markdown_text",
    "normalize_text",
    "is_font_test_text",
    "normalize_draw_segment_text",
    "assert_no_radicals_in_text",
    "normalize_category",
    "get_text_from_cell",
    "DOT_CHARS_CLASS",
    "SPACED_DOT_LEADER_RE",
    "PLAIN_DOT_LEADER_RE",
    "DOT_LEADER_EXPLOSION_RE",
    "DOT_LEADER_REPLACEMENT",
    "GREEK_SPACING_MARKS",
    "GREEK_SPACING_MARK_PLACEHOLDERS",
    "GREEK_SPACING_MARK_RESTORE",
    "VULGAR_FRACTION_CHARS",
    "VULGAR_FRACTION_PLACEHOLDERS",
    "VULGAR_FRACTION_RESTORE",
    "SCRIPT_FORM_CHARS",
    "SCRIPT_FORM_PLACEHOLDERS",
    "SCRIPT_FORM_RESTORE",
    "LATEX_SYMBOL_MAP",
    "SUPERSCRIPT_MAP",
    "SUBSCRIPT_MAP",
    "LATEX_FRACTION_COMMANDS",
    "LATEX_TEXT_COMMANDS",
    "LATEX_LAYOUT_COMMANDS",
    "LATEX_SPACE_COMMANDS",
    "LATEX_OPERATOR_MAP",
    "LATEX_VULGAR_FRACTION_MAP",
    "LATEX_FALLBACK_COMMANDS",
    "FORMULA_STRUCTURAL_COMMAND_RE",
    "FORMULA_OPERATOR_RE",
    "LATEX_CITATION_ONLY_RE",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
