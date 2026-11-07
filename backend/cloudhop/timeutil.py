import time
from datetime import datetime, timezone


def now_ms():
    return int(time.time() * 1000)


def parse_date_ms(text):
    """Milliseconds since the epoch for an ISO date or date-time, like JavaScript's Date.parse. None if invalid."""
    try:
        value = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def js_round(value):
    """Half rounds up, like JavaScript's Math.round (Python's round() rounds half to even)."""
    import math
    return math.floor(value + 0.5)
