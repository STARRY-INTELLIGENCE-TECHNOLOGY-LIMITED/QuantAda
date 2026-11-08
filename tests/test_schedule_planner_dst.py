import pandas as pd

from common.schedule_planner import SchedulePlanner


TZ = "America/New_York"


def _ts(text):
    return pd.Timestamp(text, tz=TZ)


def test_multi_day_schedule_keeps_wall_clock_in_summer_and_winter():
    parsed = SchedulePlanner.parse_schedule_rule("2d:09:30")

    summer = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-07-10 08:00"), parsed)
    winter = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-01-10 08:00"), parsed)

    assert summer == _ts("2024-07-10 09:30")
    assert winter == _ts("2024-01-10 09:30")
    assert SchedulePlanner.resolve_current_schedule_slot(_ts("2024-07-10 09:30:01"), parsed) == _ts(
        "2024-07-10 09:30"
    )


def test_multi_day_schedule_advances_across_dst_without_drift():
    parsed = SchedulePlanner.parse_schedule_rule("2d:09:30")

    slots = [
        item["slot_dt"]
        for item in SchedulePlanner.build_schedule_preview(_ts("2024-03-08 10:00"), parsed, count=3)
    ]

    assert slots == [_ts("2024-03-10 09:30"), _ts("2024-03-12 09:30"), _ts("2024-03-14 09:30")]
    assert all(slot.hour == 9 and slot.minute == 30 for slot in slots)


def test_multi_week_schedule_keeps_wall_clock_in_summer():
    parsed = SchedulePlanner.parse_schedule_rule("2w:09:30")

    slot = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-07-10 08:00"), parsed)

    assert slot == _ts("2024-07-22 09:30")
    assert SchedulePlanner.advance_schedule_slot(slot, parsed) == _ts("2024-08-05 09:30")


def test_daily_schedule_on_spring_forward_day_uses_local_time():
    parsed = SchedulePlanner.parse_schedule_rule("1d:09:30")

    current = SchedulePlanner.resolve_current_schedule_slot(_ts("2024-03-10 10:00"), parsed)
    next_slot = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-03-09 10:00"), parsed)

    assert current == _ts("2024-03-10 09:30")
    assert next_slot == _ts("2024-03-10 09:30")


def test_weekly_schedule_advance_crosses_dst_without_drift():
    parsed = SchedulePlanner.parse_schedule_rule("1w:09:30")

    slot = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-03-05 10:00"), parsed)

    assert slot == _ts("2024-03-11 09:30")
    assert SchedulePlanner.advance_schedule_slot(_ts("2024-03-04 09:30"), parsed) == slot


def test_nonexistent_and_ambiguous_wall_times_resolve_deterministically():
    spring = SchedulePlanner.parse_schedule_rule("1d:02:30")
    fall = SchedulePlanner.parse_schedule_rule("1d:01:30")

    # 春季跳过的 02:30 顺延到第一个有效时刻；秋季重复的 01:30 取第一次（EDT）。
    assert SchedulePlanner.resolve_next_schedule_slot(_ts("2024-03-10 00:00"), spring) == _ts(
        "2024-03-10 03:00"
    )
    fall_slot = SchedulePlanner.resolve_next_schedule_slot(_ts("2024-11-03 00:00"), fall)
    assert fall_slot.utcoffset() == pd.Timedelta(hours=-4)
    assert SchedulePlanner.advance_schedule_slot(fall_slot, fall) == _ts("2024-11-04 01:30")


def test_naive_calendar_interval_schedule_is_unchanged():
    parsed = SchedulePlanner.parse_schedule_rule("2d:09:30")

    slot = SchedulePlanner.resolve_next_schedule_slot(pd.Timestamp("2024-07-10 08:00"), parsed)

    assert slot == pd.Timestamp("2024-07-10 09:30")
    assert SchedulePlanner.advance_schedule_slot(slot, parsed) == pd.Timestamp("2024-07-12 09:30")
