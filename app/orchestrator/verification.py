"""Post-generation verification: does the narrative match the data?

The gap this closes: the answer model is handed a result set and writes prose
about it, and nothing ever checks that the numbers in the prose came from that
result set. A single transposed digit, a mis-attributed total, or a figure
carried over from an earlier turn is invisible — the answer reads perfectly.

This runs after generation, in Python, with no LLM. It extracts every number
from the narrative and tries to ground each one in:

  - a literal cell in the result set,
  - a column total, mean, min or max,
  - a share/percentage derivable from those totals,
  - a row count,
  - a plain restatement of something in the user's own question.

Numbers that ground nowhere are reported. The check is deliberately generous:
it is a smoke alarm for fabrication, not a proof of correctness, and a false
alarm is more expensive than a miss because it trains people to ignore it.

Verification is advisory by default — the result is logged and attached to the
response for audit, and only surfaced to the user when `strict` is on.
"""

import re
from typing import Literal

from pydantic import BaseModel

from app.core.models import QueryResult

# Numbers as written by the model: 1,234,567.89 / 36.9% / AED 1.83M / 4.2K
_NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(?:AED\s*)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|[KMB]\b)?",
    re.IGNORECASE,
)

_MULTIPLIERS = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}

# Tolerance follows the PRECISION THE MODEL WROTE, not a flat percentage.
#
# A flat relative tolerance cannot work here. It must be loose enough to accept
# "AED 66.7M" for 66,695,520 (0.007% off), which then also accepts
# "AED 18,568,036" for 18,568,630 — a transposition, exactly what we are trying
# to catch. Instead: a figure written to N significant places is checked to half
# of that place. "66.7M" is checked to +/-50,000; "18,568,036" is checked to
# +/-0.5 and the transposition is caught.
_MIN_ABS_TOLERANCE = 0.51

# Small integers are years, ranks, counts, list positions — grounding them
# produces noise, not signal.
_IGNORE_BELOW = 10.0
_YEAR_RANGE = range(1990, 2101)

_MAX_CELLS_INDEXED = 20_000


class Finding(BaseModel):
    value: float
    rendered: str
    kind: Literal["grounded", "ungrounded"]
    matched_as: str | None = None


class VerificationReport(BaseModel):
    checked: int = 0
    grounded: int = 0
    ungrounded: list[str] = []
    status: Literal["pass", "warn", "skipped"] = "skipped"

    @property
    def ratio(self) -> float:
        return self.grounded / self.checked if self.checked else 1.0

    def caveat(self) -> str | None:
        """Wording shown to the user in strict mode."""
        if self.status != "warn":
            return None
        figures = ", ".join(self.ungrounded[:3])
        return (
            f"\n\n_Note: some figures in this answer ({figures}) could not be "
            "traced back to the underlying query result. Please verify before "
            "acting on them._"
        )


def _parse(match: re.Match) -> tuple[float, str] | None:
    raw, suffix = match.group(1), (match.group(2) or "").lower()
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if suffix in _MULTIPLIERS:
        value *= _MULTIPLIERS[suffix]
    return value, match.group(0).strip()


def _precision_tolerance(rendered: str, value: float) -> float:
    """Half of the last place the model actually wrote.

    "66.7M"      -> 0.1M written  -> +/- 50,000
    "18,568,036" -> units written -> +/- 0.5
    "36.9%"      -> 0.1 written   -> +/- 0.05
    """
    text = rendered.strip()
    suffix = text[-1].lower() if text and text[-1].lower() in _MULTIPLIERS else ""
    multiplier = _MULTIPLIERS.get(suffix, 1.0)

    digits = re.sub(r"[^\d.]", "", text.rstrip("%KMBkmb "))
    if "." in digits:
        place = 10.0 ** -len(digits.split(".", 1)[1])
    else:
        place = 1.0
    return max(_MIN_ABS_TOLERANCE, place * multiplier / 2.0)


def _close(a: float, b: float, tolerance: float = _MIN_ABS_TOLERANCE) -> bool:
    return a == b or abs(a - b) <= tolerance


def _rounded_forms(value: float) -> set[float]:
    """What "AED 66.7M" could legitimately mean for 66,695,520."""
    forms = {value}
    for unit in (1_000.0, 1_000_000.0, 1_000_000_000.0):
        if abs(value) >= unit:
            forms.add(round(value / unit, 1) * unit)
            forms.add(round(value / unit, 2) * unit)
    forms.add(round(value))
    forms.add(round(value, 1))
    # A rate stored as a fraction is almost always WRITTEN as a percentage:
    # margin_pct 0.31 in the result set becomes "31%" in the narrative. Without
    # this, every correctly-reported margin, share and growth rate is flagged
    # ungrounded — the exact false-alarm class this module's docstring warns is
    # more expensive than a miss, because it teaches people to ignore the alarm.
    if 0.0 < abs(value) <= 1.0:
        as_percent = value * 100.0
        forms.add(as_percent)
        forms.add(round(as_percent, 1))
        forms.add(round(as_percent))
    return forms


def _build_ground_truth(result: QueryResult | None) -> tuple[set[float], dict[float, str]]:
    """Every number the narrative is allowed to contain, with a label for it."""
    values: set[float] = set()
    labels: dict[float, str] = {}

    def add(value: float, label: str) -> None:
        if value is None:
            return
        for form in _rounded_forms(float(value)):
            values.add(form)
            labels.setdefault(form, label)

    if result is None:
        return values, labels

    add(result.row_count, "row count")

    cells = 0
    columns_numeric: dict[int, list[float]] = {}
    for row in result.rows:
        for i, cell in enumerate(row):
            cells += 1
            if cells > _MAX_CELLS_INDEXED:
                break
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float)):
                add(cell, f"cell in {result.columns[i]}")
                columns_numeric.setdefault(i, []).append(float(cell))
            elif isinstance(cell, str):
                # Numbers embedded in text cells ("SKODA SUPERB (2016-2019)").
                for match in _NUMBER_RE.finditer(cell):
                    parsed = _parse(match)
                    if parsed:
                        add(parsed[0], f"value in {result.columns[i]}")

    for i, numbers in columns_numeric.items():
        column = result.columns[i]
        total = sum(numbers)
        add(total, f"total of {column}")
        add(total / len(numbers), f"mean of {column}")
        add(min(numbers), f"min of {column}")
        add(max(numbers), f"max of {column}")
        add(len(numbers), f"count of {column}")
        # Shares of the total, which is how the model phrases concentration.
        if total:
            for value in numbers:
                add(value / total * 100.0, f"share of {column}")
            ranked = sorted(numbers, reverse=True)
            for n in (3, 5, 10):
                if len(ranked) > n:
                    add(sum(ranked[:n]), f"top-{n} of {column}")
                    add(sum(ranked[:n]) / total * 100.0, f"top-{n} share of {column}")
        # Differences between consecutive ranked values ("3.6x the next largest")
        if len(numbers) > 1:
            ranked = sorted(numbers, reverse=True)
            if ranked[1]:
                add(ranked[0] / ranked[1], f"ratio within {column}")

    return values, labels


def verify(
    answer: str,
    result: QueryResult | None,
    question: str = "",
    strict: bool = False,
) -> VerificationReport:
    """Ground every figure in ``answer`` against ``result``."""
    if result is None or not result.rows:
        return VerificationReport(status="skipped")

    truth, labels = _build_ground_truth(result)
    # Numbers the user themselves supplied are fair game to repeat.
    question_numbers = {
        parsed[0]
        for match in _NUMBER_RE.finditer(question)
        if (parsed := _parse(match))
    }

    checked = 0
    grounded = 0
    ungrounded: list[str] = []
    seen: set[str] = set()

    for match in _NUMBER_RE.finditer(answer):
        parsed = _parse(match)
        if parsed is None:
            continue
        value, rendered = parsed
        if abs(value) < _IGNORE_BELOW:
            continue
        if value.is_integer() and int(value) in _YEAR_RANGE:
            continue
        if rendered in seen:
            continue
        seen.add(rendered)

        checked += 1
        tolerance = _precision_tolerance(rendered, value)
        if any(_close(value, t, tolerance) for t in question_numbers):
            grounded += 1
            continue
        if any(_close(value, t, tolerance) for t in truth):
            grounded += 1
            continue
        ungrounded.append(rendered)

    if checked == 0:
        return VerificationReport(status="skipped")

    report = VerificationReport(
        checked=checked,
        grounded=grounded,
        ungrounded=ungrounded[:10],
        status="warn" if ungrounded and strict else ("warn" if ungrounded else "pass"),
    )
    return report
