from __future__ import annotations
import datetime
from livetest.ggtrail.julian import to_julian_long, from_julian_long


def test_decodes_real_rt000000_creationtime():
    dt = from_julian_long(0x02F216D3295357E1)
    assert dt == datetime.datetime(2014, 1, 23, 9, 17, 7, 72000)


def test_decodes_real_rt000002_creationtime():
    dt = from_julian_long(0x02F216D3599EE2EE)
    assert dt == datetime.datetime(2014, 1, 23, 9, 30, 37, 330000)


def test_round_trips_arbitrary_datetime():
    dt = datetime.datetime(2026, 7, 14, 12, 34, 56, 789000)
    assert from_julian_long(to_julian_long(dt)) == dt


def test_round_trips_epoch():
    dt = datetime.datetime(1970, 1, 1, 0, 0, 0, 0)
    assert from_julian_long(to_julian_long(dt)) == dt
