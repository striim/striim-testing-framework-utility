from __future__ import annotations
import datetime

# GG trail 8-byte "Julian long" timestamp -- exact inverse of ByteUtil.julianLongToDate
# (GGTrailParser src/main/java/com/webaction/source/gg/util/ByteUtil.java:673-696).
# Verified against real trail fixtures: rt000000's CREATIONTIME (0x02F216D3295357E1)
# decodes to 2014-01-23 09:17:07.072 UTC and rt000002's to 2014-01-23 09:30:37.330 UTC,
# both matching sample.def's "Definitions created/modified 2014-01-23 15:04" comment.
_DAYS_SINCE_4713BC_TO_1970 = int((4712 + 1970) * 365.25) - 12  # == 2440588
_EPOCH = datetime.datetime(1970, 1, 1)


def to_julian_long(dt: datetime.datetime) -> int:
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    target_millis = int((dt - _EPOCH).total_seconds() * 1000)
    return _millis_to_julian_long(target_millis)


def _millis_to_julian_long(target_millis: int) -> int:
    milli = target_millis % 1000
    total_secs = target_millis // 1000
    secs = total_secs % 60
    total_mins = total_secs // 60
    mins = total_mins % 60
    total_hours = total_mins // 60
    days_since_epoch = (total_hours - 12) // 24
    hours = (total_hours - 12) % 24
    e = days_since_epoch + _DAYS_SINCE_4713BC_TO_1970
    d = e * 24 + hours
    c = d * 60 + mins
    b = c * 60 + secs
    t1 = b * 1000 + milli
    return t1 * 1000  # low 3 decimal digits always 0 -- discarded on decode anyway


def from_julian_long(ts: int) -> datetime.datetime:
    j = ts
    j //= 1000
    milli = j % 1000
    j //= 1000
    secs = j % 60
    j //= 60
    mins = j % 60
    j //= 60
    hours = j % 24
    j //= 24
    j -= _DAYS_SINCE_4713BC_TO_1970
    j *= 24
    j += hours + 12
    j *= 60
    j += mins
    j *= 60
    j += secs
    j *= 1000
    j += milli
    return _EPOCH + datetime.timedelta(milliseconds=j)
