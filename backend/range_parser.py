from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime


MONTH_LOOKUP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
}


@dataclass(frozen=True)
class RangeParse:
    token: str
    explicit: bool
    chart_months: int | None = None
    unsupported_reason: str = ""


def words(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for char in (text or "").lower():
        if char.isalnum():
            current.append(char)
        else:
            if current:
                chunks.append("".join(current))
                current = []
    if current:
        chunks.append("".join(current))
    return chunks


def contains(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    if not phrase or len(phrase) > len(tokens):
        return False
    width = len(phrase)
    return any(tuple(tokens[idx:idx + width]) == phrase for idx in range(len(tokens) - width + 1))


def _int_token(token: str) -> int | None:
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    if token in NUMBER_WORDS:
        return NUMBER_WORDS[token]
    return None


def _amount_at(tokens: list[str], idx: int) -> tuple[int, int] | None:
    if idx >= len(tokens):
        return None
    first = _int_token(tokens[idx])
    if first is None:
        return None
    if idx + 1 < len(tokens) and first in {20, 30}:
        second = _int_token(tokens[idx + 1])
        if second and 1 <= second <= 9:
            return first + second, idx + 2
    return first, idx + 1


def _month_count_since(tokens: list[str], now: datetime) -> int | None:
    for idx, token in enumerate(tokens[:-1]):
        if token != "since":
            continue
        month = MONTH_LOOKUP.get(tokens[idx + 1])
        if month is None:
            continue
        year = now.year
        if idx + 2 < len(tokens):
            maybe_year = _int_token(tokens[idx + 2])
            if maybe_year and 2000 <= maybe_year <= 2099:
                year = maybe_year
        elif month > now.month:
            year -= 1
        end_year, end_month = (now.year, now.month)
        if now.day == 1:
            end_year, end_month = _shift_month(now.year, now.month, -1)
        return max(1, min((end_year - year) * 12 + end_month - month + 1, 36))
    return None


def _explicit_month(tokens: list[str]) -> str | None:
    for idx, token in enumerate(tokens[:-1]):
        year = _int_token(token)
        month = _int_token(tokens[idx + 1])
        if year and month and 2000 <= year <= 2099 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    return None


def _month_token(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_bounds_token(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01..{year:04d}-{month:02d}-{last_day:02d}"


def _bounded_range_token(start_year: int, start_month: int, end_year: int, end_month: int) -> str:
    last_day = calendar.monthrange(end_year, end_month)[1]
    return f"{start_year:04d}-{start_month:02d}-01..{end_year:04d}-{end_month:02d}-{last_day:02d}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _coerce_month_token(token: str, now: datetime) -> str | None:
    token = (token or "").strip().lower()
    if token in {"current_month", "this_month", "current"}:
        return _month_token(now.year, now.month)
    if token in {"last_month", "prior_month", "previous_month", "prior"}:
        return _month_token(*_shift_month(now.year, now.month, -1))
    if len(token) == 7 and token[4] == "-":
        try:
            year = int(token[:4])
            month = int(token[5:7])
        except ValueError:
            return None
        if 1 <= month <= 12:
            return _month_token(year, month)
    return None


def _explicit_named_month(tokens: list[str], now: datetime) -> str | None:
    for idx, token in enumerate(tokens):
        month = MONTH_LOOKUP.get(token)
        if month is None:
            continue
        year: int | None = None
        if idx + 1 < len(tokens):
            maybe_year = _int_token(tokens[idx + 1])
            if maybe_year and 2000 <= maybe_year <= 2099:
                year = maybe_year
        if year is None and idx > 0:
            maybe_year = _int_token(tokens[idx - 1])
            if maybe_year and 2000 <= maybe_year <= 2099:
                year = maybe_year
        if year is None:
            year = now.year if month <= now.month else now.year - 1
        return _month_token(year, month)
    return None


def _unsupported_bounded_range(tokens: list[str]) -> str:
    if _has_between_months(tokens):
        return "custom date ranges are not supported yet"
    if _has_quarter_phrase(tokens):
        return "quarter ranges are not supported yet"
    return ""


def _year_near(tokens: list[str], idx: int) -> int | None:
    for pos in (idx + 1, idx - 1):
        if 0 <= pos < len(tokens):
            year = _int_token(tokens[pos])
            if year and 2000 <= year <= 2099:
                return year
    return None


def _between_months_range(tokens: list[str], now: datetime) -> str | None:
    connectors = {"and", "to", "through", "thru", "until"}
    month_positions = [idx for idx, item in enumerate(tokens) if MONTH_LOOKUP.get(item)]
    for left, right in zip(month_positions, month_positions[1:]):
        if not connectors & set(tokens[left + 1 : right]):
            continue
        start_month = MONTH_LOOKUP[tokens[left]]
        end_month = MONTH_LOOKUP[tokens[right]]
        if start_month is None or end_month is None:
            continue

        shared_year = next(
            (
                year
                for year in (_int_token(token) for token in tokens)
                if year and 2000 <= year <= 2099
            ),
            None,
        )
        start_year = _year_near(tokens, left) or shared_year
        end_year = _year_near(tokens, right) or shared_year
        if start_year is None and end_year is None:
            if start_month > now.month and end_month > now.month:
                start_year = now.year - 1
            else:
                start_year = now.year
        if start_year is None:
            start_year = end_year
        if end_year is None:
            end_year = start_year
        if end_month < start_month and end_year <= start_year:
            end_year = start_year + 1
        return _bounded_range_token(start_year, start_month, end_year, end_month)
    return None


def _quarter_range(tokens: list[str], now: datetime) -> str | None:
    token_set = set(tokens)
    quarter: int | None = None
    year = next(
        (
            value
            for value in (_int_token(token) for token in tokens)
            if value and 2000 <= value <= 2099
        ),
        None,
    )
    for token in tokens:
        if token in {"q1", "q2", "q3", "q4"}:
            quarter = int(token[1])
            break
    if quarter is None:
        if not ({"quarter", "qtr"} & token_set):
            return None
        current_quarter = ((now.month - 1) // 3) + 1
        if {"previous", "prior", "last"} & token_set:
            quarter = current_quarter - 1
            year = year or now.year
            if quarter < 1:
                quarter = 4
                year -= 1
        elif {"this", "current"} & token_set:
            quarter = current_quarter
            year = year or now.year
        else:
            return None
    year = year or now.year
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    if {"this", "current"} & token_set and quarter == ((now.month - 1) // 3) + 1 and year == now.year:
        return f"{year:04d}-{start_month:02d}-01..{now.year:04d}-{now.month:02d}-{now.day:02d}"
    return _bounded_range_token(year, start_month, year, end_month)


def _has_between_months(tokens: list[str]) -> bool:
    connectors = {"and", "to", "through", "thru", "until"}
    month_positions = [idx for idx, item in enumerate(tokens) if MONTH_LOOKUP.get(item)]
    if len(month_positions) >= 2:
        left, right = month_positions[0], month_positions[1]
        if connectors & set(tokens[left + 1 : right]):
            return True
    for idx, token in enumerate(tokens):
        if token not in {"between", "from"}:
            continue
        remaining = tokens[idx + 1 :]
        remaining_month_positions = [pos for pos, item in enumerate(remaining) if MONTH_LOOKUP.get(item)]
        if len(remaining_month_positions) < 2:
            continue
        left, right = remaining_month_positions[0], remaining_month_positions[1]
        if connectors & set(remaining[left + 1 : right]):
            return True
    return False


def _has_quarter_phrase(tokens: list[str]) -> bool:
    token_set = set(tokens)
    if {"quarter", "qtr"} & token_set:
        return True
    return any(token in {"q1", "q2", "q3", "q4"} for token in tokens)


def _relative_month_delta(tokens: list[str]) -> int | None:
    if (
        contains(tokens, ("month", "before"))
        or contains(tokens, ("the", "month", "before"))
        or contains(tokens, ("one", "month", "earlier"))
        or contains(tokens, ("month", "earlier"))
        or contains(tokens, ("previous", "month"))
        or contains(tokens, ("prior", "month"))
    ):
        return -1
    if (
        contains(tokens, ("next", "month"))
        or contains(tokens, ("month", "after"))
        or contains(tokens, ("the", "month", "after"))
        or contains(tokens, ("one", "month", "later"))
        or contains(tokens, ("month", "later"))
    ):
        return 1
    return None


def parse_range(question: str, *, default: str = "current_month", now: datetime | None = None) -> RangeParse:
    now = now or datetime.now()
    tokens = words(question)
    token_set = set(tokens)

    bounded = _between_months_range(tokens, now) or _quarter_range(tokens, now)
    if bounded:
        return RangeParse(bounded, True)

    unsupported = _unsupported_bounded_range(tokens)
    if unsupported:
        return RangeParse("", True, unsupported_reason=unsupported)

    explicit_month = _explicit_month(tokens)
    if explicit_month:
        return RangeParse(explicit_month, True)

    since_months = _month_count_since(tokens, now)
    if since_months is not None:
        return RangeParse(f"last_{since_months}_months", True, chart_months=since_months)

    named_month = _explicit_named_month(tokens, now)
    if named_month:
        return RangeParse(named_month, True, chart_months=1)

    if contains(tokens, ("all", "time")) or {"alltime", "ever", "lifetime"} & token_set:
        return RangeParse("all", True)
    if {"ytd"} & token_set or contains(tokens, ("year", "to", "date")) or contains(tokens, ("this", "year")):
        return RangeParse("ytd", True)
    if contains(tokens, ("till", "now")) or contains(tokens, ("until", "now")) or contains(tokens, ("to", "date")):
        return RangeParse("all", True)

    if contains(tokens, ("past", "year")):
        return RangeParse("last_12_months", True, chart_months=12)
    if contains(tokens, ("over", "the", "past", "year")) or contains(tokens, ("last", "12", "months")):
        return RangeParse("last_12_months", True, chart_months=12)
    if contains(tokens, ("last", "year")) or contains(tokens, ("previous", "year")) or contains(tokens, ("prior", "year")):
        return RangeParse("last_year", True)

    if contains(tokens, ("this", "month")) or contains(tokens, ("current", "month")):
        return RangeParse("current_month", True, chart_months=1)
    if contains(tokens, ("last", "month")) or contains(tokens, ("previous", "month")) or contains(tokens, ("prior", "month")):
        return RangeParse("last_month", True, chart_months=1)

    relative_delta = _relative_month_delta(tokens)
    if relative_delta is not None:
        if relative_delta < 0:
            return RangeParse("last_month", True, chart_months=1)
        year, month = _shift_month(now.year, now.month, relative_delta)
        return RangeParse(_month_token(year, month), True, chart_months=1)
    if contains(tokens, ("this", "week")) or contains(tokens, ("current", "week")):
        return RangeParse("this_week", True)
    if contains(tokens, ("last", "week")) or contains(tokens, ("previous", "week")) or contains(tokens, ("prior", "week")):
        return RangeParse("last_week", True)

    if contains(tokens, ("half", "year")) or contains(tokens, ("half", "a", "year")):
        return RangeParse("last_6_months", True, chart_months=6)

    range_heads = {"last", "past", "previous", "prior"}
    for idx, token in enumerate(tokens[:-1]):
        if token not in range_heads and not (token == "over" and idx + 2 < len(tokens) and tokens[idx + 1] == "the"):
            continue
        number_idx = idx + 1 if token != "over" else idx + 3
        if number_idx >= len(tokens):
            continue
        amount_span = _amount_at(tokens, number_idx)
        if amount_span is None:
            continue
        amount, unit_idx = amount_span
        unit = tokens[unit_idx] if unit_idx < len(tokens) else ""
        if unit in {"month", "months"}:
            months = max(1, min(amount, 36))
            return RangeParse(f"last_{months}_months", True, chart_months=months)
        if unit in {"day", "days"}:
            days = max(1, min(amount, 365))
            return RangeParse(f"last_{days}d", True)
        if unit in {"week", "weeks"}:
            days = max(1, min(amount * 7, 365))
            return RangeParse(f"last_{days}d", True)

    if contains(tokens, ("so", "far")):
        return RangeParse("all", True)

    return RangeParse(default, False)


def resolve_followup_range(text: str, prior_range: str | None, now: datetime | None = None) -> RangeParse:
    """Resolve a follow-up range, allowing relative month language to use the prior answer range."""
    now = now or datetime.now()
    tokens = words(text)
    delta = _relative_month_delta(tokens)
    if delta is not None:
        base = _coerce_month_token(prior_range or "", now) or _coerce_month_token("current_month", now)
        year = int(base[:4])
        month = int(base[5:7])
        shifted_year, shifted_month = _shift_month(year, month, delta)
        return RangeParse(_month_token(shifted_year, shifted_month), True, chart_months=1)
    return parse_range(text, now=now)


def has_explicit_time_scope(question: str) -> bool:
    return parse_range(question).explicit


def chart_months(question: str, fallback: int = 6) -> int:
    parsed = parse_range(question)
    if parsed.chart_months:
        return parsed.chart_months
    if not parsed.explicit:
        return fallback
    if parsed.token == "last_year" or parsed.token == "last_12_months":
        return 12
    return fallback
