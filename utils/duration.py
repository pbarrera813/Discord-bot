import re


class DurationParseError(ValueError):
    pass


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdSMHD])\s*$")


def parse_duration(duration_raw: str) -> tuple[int, str]:
    match = _DURATION_RE.match(duration_raw)
    if not match:
        raise DurationParseError(
            "Invalid duration format. Use values like 120s, 2m, 3h, 1d."
        )

    value = int(match.group(1))
    unit = match.group(2).lower()
    pretty = duration_raw.strip()

    if value <= 0:
        raise DurationParseError("Duration must be greater than zero.")

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }
    return value * multipliers[unit], pretty
