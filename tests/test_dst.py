"""Price slots must stay aligned with the market on the two odd days a year.

Nordpool publishes 92 quarters on the spring-forward day and 100 on the
fall-back day. Naive `midnight + i x interval` arithmetic assumes 96, so from
the transition onward every slot was priced from the wrong hour — the whole
rest of the day, twice a year.
"""

from datetime import date, datetime, time, timedelta

import pytest


@pytest.fixture
def stockholm(monkeypatch, pkg_loader):
    """Point the planner's timezone helper at a real DST-observing zone."""
    from zoneinfo import ZoneInfo

    planner = pkg_loader("planner")
    monkeypatch.setattr(planner, "_local_timezone", lambda: ZoneInfo("Europe/Stockholm"))
    return planner


def test_spring_forward_day_covers_the_whole_day(stockholm):
    """23 hours of quarters must still reach 23:45, not stop an hour short."""
    starts = stockholm.slot_start_times(date(2026, 3, 29), 92, 15)

    assert len(starts) == 92
    assert starts[0].strftime("%H:%M") == "00:00"
    assert starts[-1].strftime("%H:%M") == "23:45"

    # The naive arithmetic this replaced would have ended here instead.
    midnight = datetime.combine(date(2026, 3, 29), time(0, 0))
    naive_last = midnight + timedelta(minutes=91 * 15)
    assert naive_last.strftime("%H:%M") == "22:45"


def test_spring_forward_skips_the_missing_hour(stockholm):
    """02:00-02:59 does not exist that day and must not appear."""
    starts = stockholm.slot_start_times(date(2026, 3, 29), 92, 15)
    labels = [s.strftime("%H:%M") for s in starts]

    assert "01:45" in labels
    assert "03:00" in labels
    assert not any(l.startswith("02:") for l in labels)


def test_fall_back_day_covers_the_whole_day(stockholm):
    """25 hours of quarters must not run over into the next day."""
    starts = stockholm.slot_start_times(date(2026, 10, 25), 100, 15)

    assert len(starts) == 100
    assert starts[0].strftime("%H:%M") == "00:00"
    assert starts[-1].strftime("%H:%M") == "23:45"
    # The repeated hour appears twice, which is what a 100-slot day means.
    assert [s.strftime("%H:%M") for s in starts].count("02:00") == 2


def test_ordinary_day_is_unchanged(stockholm):
    starts = stockholm.slot_start_times(date(2026, 8, 30), 96, 15)
    midnight = datetime.combine(date(2026, 8, 30), time(0, 0))

    assert starts == [midnight + timedelta(minutes=i * 15) for i in range(96)]


def test_hourly_prices_work_too(stockholm):
    starts = stockholm.slot_start_times(date(2026, 3, 29), 23, 60)

    assert len(starts) == 23
    assert starts[-1].strftime("%H:%M") == "23:00"


def test_falls_back_to_naive_without_a_timezone(pkg_loader, monkeypatch):
    """Standalone use, or Home Assistant before its timezone is configured."""
    planner = pkg_loader("planner")
    monkeypatch.setattr(planner, "_local_timezone", lambda: None)

    starts = planner.slot_start_times(date(2026, 3, 29), 96, 15)
    midnight = datetime.combine(date(2026, 3, 29), time(0, 0))

    assert starts == [midnight + timedelta(minutes=i * 15) for i in range(96)]


def test_calendar_event_offset_is_converted_not_dropped(pkg_loader, monkeypatch):
    """A UTC timestamp must become local time, not lose its offset."""
    from zoneinfo import ZoneInfo

    planner = pkg_loader("planner")
    monkeypatch.setattr(planner, "_local_timezone", lambda: ZoneInfo("Europe/Stockholm"))

    # 05:30 UTC in August is 07:30 in Stockholm (CEST, +02:00).
    events = [{"start": "2026-08-30T05:30:00+00:00", "summary": "Depart 90%"}]
    start, target = planner.get_calendar_data(events, datetime(2026, 8, 30, 4, 0))

    assert start.strftime("%H:%M") == "07:30"
    assert target == 90
