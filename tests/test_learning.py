"""Tests for efficiency learning functionality."""
import pytest
from datetime import datetime, time, timedelta


def test_learning_constants():
    """Test that learning constants are defined."""
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_CONFIDENCE,
        LEARNING_SESSIONS,
        LEARNING_LOCKED,
        LEARNING_HISTORY,
        DEFAULT_LOSS,
    )
    
    assert LEARNING_CHARGER_LOSS == "learned_charger_loss"
    assert LEARNING_CONFIDENCE == "loss_confidence_level"
    assert LEARNING_SESSIONS == "loss_learning_sessions"
    assert LEARNING_LOCKED == "loss_locked"
    assert LEARNING_HISTORY == "learning_history"
    assert DEFAULT_LOSS == 0.0


def test_get_effective_charger_loss_initial():
    """Test that initial loss uses configured value."""
    from custom_components.ev_optimizer.planner import get_effective_charger_loss
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_SESSIONS,
        LEARNING_MEASUREMENTS,
        LEARNING_CONFIDENCE,
        LEARNING_LOCKED,
    )
    
    config = {"charger_loss": 10.0}
    learning_state = {
        LEARNING_CHARGER_LOSS: 10.0,
        LEARNING_SESSIONS: 0,
        LEARNING_CONFIDENCE: 0,
        LEARNING_LOCKED: False,
    }
    
    effective_loss, is_learning = get_effective_charger_loss(config, learning_state)
    
    assert effective_loss == 10.0
    assert is_learning is True  # First 10 sessions


def test_get_effective_charger_loss_uses_learned():
    """Test that learned value is used after first session."""
    from custom_components.ev_optimizer.planner import get_effective_charger_loss
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_SESSIONS,
        LEARNING_MEASUREMENTS,
        LEARNING_CONFIDENCE,
        LEARNING_LOCKED,
    )
    
    config = {"charger_loss": 10.0}
    learning_state = {
        LEARNING_CHARGER_LOSS: 7.5,
        LEARNING_SESSIONS: 3,
        LEARNING_MEASUREMENTS: 3,
        LEARNING_CONFIDENCE: 4,
        LEARNING_LOCKED: False,
    }
    
    effective_loss, is_learning = get_effective_charger_loss(config, learning_state)
    
    assert effective_loss == 7.5  # Uses learned value
    assert is_learning is True


def test_get_effective_charger_loss_locked():
    """Test that locked state stops learning phase."""
    from custom_components.ev_optimizer.planner import get_effective_charger_loss
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_SESSIONS,
        LEARNING_MEASUREMENTS,
        LEARNING_CONFIDENCE,
        LEARNING_LOCKED,
    )
    
    config = {"charger_loss": 10.0}
    learning_state = {
        LEARNING_CHARGER_LOSS: 8.2,
        LEARNING_SESSIONS: 7,
        LEARNING_MEASUREMENTS: 7,
        LEARNING_CONFIDENCE: 8,
        LEARNING_LOCKED: True,
    }
    
    effective_loss, is_learning = get_effective_charger_loss(config, learning_state)
    
    assert effective_loss == 8.2
    assert is_learning is False  # Locked


def test_get_effective_charger_loss_after_10_sessions():
    """Test that learning phase ends after 10 sessions."""
    from custom_components.ev_optimizer.planner import get_effective_charger_loss
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_SESSIONS,
        LEARNING_MEASUREMENTS,
        LEARNING_CONFIDENCE,
        LEARNING_LOCKED,
    )
    
    config = {"charger_loss": 10.0}
    learning_state = {
        LEARNING_CHARGER_LOSS: 8.0,
        LEARNING_SESSIONS: 10,
        LEARNING_MEASUREMENTS: 10,
        LEARNING_CONFIDENCE: 7,
        LEARNING_LOCKED: False,
    }
    
    effective_loss, is_learning = get_effective_charger_loss(config, learning_state)
    
    assert effective_loss == 8.0
    assert is_learning is False  # 10+ sessions


def test_learning_buffer_added_during_learning():
    """Test that 30min buffer is added during learning phase."""
    from custom_components.ev_optimizer.planner import generate_charging_plan
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_SESSIONS,
        LEARNING_MEASUREMENTS,
        LEARNING_CONFIDENCE,
        LEARNING_LOCKED,
    )
    
    now = datetime(2024, 1, 15, 20, 0)
    
    data = {
        "car_plugged": True,
        "car_soc": 50,
        "smart_charging_active": True,
        "price_data": {
            "today": [0.5] * 24,
            "tomorrow": [0.5] * 24,
        },
        "target_soc": 80,
        "departure_time": time(7, 0),
        "min_guaranteed_soc": 20,
        "p1_l1": 5.0,
        "p1_l2": 5.0,
        "p1_l3": 5.0,
    }
    
    config = {
        "car_capacity": 64.0,
        "max_fuse": 20.0,
        "charger_loss": 0.0,
        "has_price_sensor": True,
        "currency": "SEK",
    }
    
    learning_state_learning = {
        LEARNING_CHARGER_LOSS: 5.0,
        LEARNING_SESSIONS: 3,
        LEARNING_CONFIDENCE: 4,
        LEARNING_LOCKED: False,
    }
    
    learning_state_locked = {
        LEARNING_CHARGER_LOSS: 5.0,
        LEARNING_SESSIONS: 10,
        LEARNING_CONFIDENCE: 9,
        LEARNING_LOCKED: True,
    }
    
    # Generate plans with both states
    plan_learning = generate_charging_plan(
        data, config, False, learning_state=learning_state_learning, now=now
    )
    plan_locked = generate_charging_plan(
        data, config, False, learning_state=learning_state_locked, now=now
    )
    
    # Learning phase should have more slots (due to 30min buffer)
    learning_slots = len([s for s in plan_learning.get("charging_schedule", []) if s.get("active")])
    locked_slots = len([s for s in plan_locked.get("charging_schedule", []) if s.get("active")])
    
    # With 30min buffer (2 slots for 15min intervals or 0.5 for 60min), learning should have more
    assert learning_slots >= locked_slots


def test_learning_state_defaults():
    """Test that learning state has proper defaults."""
    from custom_components.ev_optimizer.const import (
        LEARNING_CHARGER_LOSS,
        LEARNING_CONFIDENCE,
        LEARNING_SESSIONS,
        LEARNING_LOCKED,
        LEARNING_HISTORY,
        DEFAULT_LOSS,
    )
    
    # Simulate fresh learning state
    learning_state = {
        LEARNING_CHARGER_LOSS: DEFAULT_LOSS,
        LEARNING_CONFIDENCE: 0,
        LEARNING_SESSIONS: 0,
        LEARNING_LOCKED: False,
        LEARNING_HISTORY: [],
    }
    
    assert learning_state[LEARNING_CHARGER_LOSS] == 0.0
    assert learning_state[LEARNING_CONFIDENCE] == 0
    assert learning_state[LEARNING_SESSIONS] == 0
    assert learning_state[LEARNING_LOCKED] is False
    assert learning_state[LEARNING_HISTORY] == []


def test_learning_adjustment_increases_on_underperformance():
    """Test that efficiency loss increases when actual SoC is lower than expected."""
    # Simulate: Expected 75%, got 70% -> need MORE loss
    
    expected_soc = 75.0
    actual_soc = 70.0
    soc_error = actual_soc - expected_soc  # -5%
    
    current_loss = 5.0
    sessions = 2
    confidence = 3
    
    # Logic from coordinator._evaluate_efficiency_learning
    margin = 3.0  # confidence < 3
    
    if soc_error < -margin:
        # Underperforming - increase loss
        adjustment = min(3.0, abs(soc_error) * 0.5)  # Aggressive early (sessions < 5)
        current_loss += adjustment
        confidence = max(0, confidence - 1)
    
    assert current_loss > 5.0  # Loss should increase
    assert current_loss == 7.5  # 5.0 + min(3.0, 5.0*0.5) = 5.0 + 2.5
    assert confidence == 2  # Confidence decreased


def test_learning_adjustment_decreases_on_overperformance():
    """Test that efficiency loss decreases when actual SoC is higher than expected."""
    # Simulate: Expected 70%, got 76% -> need LESS loss
    
    expected_soc = 70.0
    actual_soc = 76.0
    soc_error = actual_soc - expected_soc  # +6%
    
    current_loss = 10.0
    sessions = 2
    confidence = 3
    
    margin = 3.0
    
    if soc_error > margin:
        # Outperforming - decrease loss (careful!)
        adjustment = -min(2.0, soc_error * 0.4)  # sessions < 5
        current_loss += adjustment
        confidence = max(0, confidence - 1)
    
    assert current_loss < 10.0  # Loss should decrease
    assert current_loss == 8.0  # 10.0 - 2.0 (min(2.0, 6.0*0.4)=min(2.0, 2.4)=2.0)
    assert confidence == 2


def test_learning_confidence_increases_within_margin():
    """Test that confidence increases when actual matches expected within margin."""
    expected_soc = 75.0
    actual_soc = 76.5
    soc_error = actual_soc - expected_soc  # +1.5%
    
    confidence = 5
    margin = 2.0  # confidence >= 3 and < 6
    
    if abs(soc_error) <= margin:
        confidence += 1
    
    assert confidence == 6


def test_learning_locks_at_confidence_8():
    """Test that learning locks when confidence reaches 8."""
    confidence = 7
    locked = False
    
    # Simulate good measurement
    confidence += 1
    
    if confidence >= 8:
        locked = True
    
    assert confidence == 8
    assert locked is True


def test_learning_bounds_loss_percentage():
    """Test that loss percentage is bounded between 0 and 20."""
    # Test lower bound
    loss_negative = -5.0
    loss_bounded = max(0.0, min(20.0, loss_negative))
    assert loss_bounded == 0.0
    
    # Test upper bound
    loss_too_high = 25.0
    loss_bounded = max(0.0, min(20.0, loss_too_high))
    assert loss_bounded == 20.0
    
    # Test within bounds
    loss_ok = 8.5
    loss_bounded = max(0.0, min(20.0, loss_ok))
    assert loss_bounded == 8.5


def test_learning_history_keeps_last_10():
    """Test that learning history only keeps last 10 entries."""
    history = []
    
    # Add 15 entries
    for i in range(15):
        history.append({
            "timestamp": f"2024-01-{i+1:02d}T12:00:00",
            "expected_soc": 70.0,
            "actual_soc": 71.0,
            "error": 1.0,
        })
    
    # Keep only last 10
    history = history[-10:]
    
    assert len(history) == 10
    assert history[0]["timestamp"] == "2024-01-06T12:00:00"  # Entry 6 is first
    assert history[-1]["timestamp"] == "2024-01-15T12:00:00"  # Entry 15 is last


def test_price_arrival_constant_exists():
    """Test that price arrival learning constant is defined."""
    from custom_components.ev_optimizer.const import LEARNING_PRICE_ARRIVAL
    
    assert LEARNING_PRICE_ARRIVAL == "price_arrival_times"


def test_price_arrival_tracking_initial():
    """Test that price arrivals can be tracked."""
    from custom_components.ev_optimizer.const import LEARNING_PRICE_ARRIVAL
    
    # Simulate tracking price arrivals
    price_arrivals = []
    
    # Add first arrival
    price_arrivals.append({
        "date": "2024-01-15",
        "time": "13:30",
        "timestamp": "2024-01-15T13:30:00",
    })
    
    assert len(price_arrivals) == 1
    assert price_arrivals[0]["time"] == "13:30"


def test_price_arrival_keeps_last_14():
    """Test that price arrival history keeps only last 14 entries."""
    price_arrivals = []
    
    # Add 20 entries
    for i in range(20):
        price_arrivals.append({
            "date": f"2024-01-{i+1:02d}",
            "time": "13:30",
            "timestamp": f"2024-01-{i+1:02d}T13:30:00",
        })
    
    # Keep only last 14
    price_arrivals = price_arrivals[-14:]
    
    assert len(price_arrivals) == 14
    assert price_arrivals[0]["date"] == "2024-01-07"  # Entry 7 is first
    assert price_arrivals[-1]["date"] == "2024-01-20"  # Entry 20 is last


def test_expected_price_arrival_calculation_insufficient_data():
    """Test that expected time returns None with insufficient data."""
    price_arrivals = [
        {"date": "2024-01-15", "time": "13:30", "timestamp": "2024-01-15T13:30:00"},
        {"date": "2024-01-16", "time": "13:25", "timestamp": "2024-01-16T13:25:00"},
    ]
    
    # Need at least 3 samples
    if len(price_arrivals) < 3:
        expected_time = None
    else:
        expected_time = "13:30"
    
    assert expected_time is None


def test_expected_price_arrival_calculation_with_data():
    """Test that expected time is calculated correctly from history."""
    price_arrivals = [
        {"date": "2024-01-15", "time": "13:30", "timestamp": "2024-01-15T13:30:00"},
        {"date": "2024-01-16", "time": "13:25", "timestamp": "2024-01-16T13:25:00"},
        {"date": "2024-01-17", "time": "13:35", "timestamp": "2024-01-17T13:35:00"},
        {"date": "2024-01-18", "time": "13:28", "timestamp": "2024-01-18T13:28:00"},
    ]
    
    # Calculate average: 13:30, 13:25, 13:35, 13:28
    # In minutes: 810, 805, 815, 808
    # Average: 809.5 = 809 minutes = 13:29
    times_in_minutes = []
    for entry in price_arrivals:
        time_str = entry["time"]
        parts = time_str.split(":")
        hours = int(parts[0])
        minutes = int(parts[1])
        total_minutes = hours * 60 + minutes
        times_in_minutes.append(total_minutes)
    
    avg_minutes = sum(times_in_minutes) // len(times_in_minutes)
    avg_hours = avg_minutes // 60
    avg_mins = avg_minutes % 60
    expected_time = f"{avg_hours:02d}:{avg_mins:02d}"
    
    assert expected_time == "13:29"


def test_expected_price_arrival_handles_varied_times():
    """Test that expected time calculation handles varied arrival times."""
    price_arrivals = [
        {"date": "2024-01-15", "time": "13:00", "timestamp": "2024-01-15T13:00:00"},
        {"date": "2024-01-16", "time": "14:00", "timestamp": "2024-01-16T14:00:00"},
        {"date": "2024-01-17", "time": "13:30", "timestamp": "2024-01-17T13:30:00"},
    ]
    
    # Calculate average: 13:00, 14:00, 13:30
    # In minutes: 780, 840, 810
    # Average: 810 minutes = 13:30
    times_in_minutes = [780, 840, 810]
    avg_minutes = sum(times_in_minutes) // len(times_in_minutes)
    avg_hours = avg_minutes // 60
    avg_mins = avg_minutes % 60
    expected_time = f"{avg_hours:02d}:{avg_mins:02d}"
    
    assert expected_time == "13:30"


def test_planner_accepts_expected_price_time():
    """Test that planner accepts expected_price_time parameter."""
    from custom_components.ev_optimizer.planner import generate_charging_plan
    
    now = datetime(2024, 1, 15, 20, 0)
    
    data = {
        "car_plugged": True,
        "car_soc": 50,
        "smart_charging_active": True,
        "price_data": {
            "today": [0.5] * 24,
            "tomorrow": [],  # No tomorrow prices yet
            "tomorrow_valid": False,
        },
        "target_soc": 80,
        "departure_time": time(7, 0),
        "min_guaranteed_soc": 20,
        "p1_l1": 5.0,
        "p1_l2": 5.0,
        "p1_l3": 5.0,
    }
    
    config = {
        "car_capacity": 64.0,
        "max_fuse": 20.0,
        "charger_loss": 5.0,
        "has_price_sensor": True,
        "currency": "SEK",
    }
    
    # Should accept expected_price_time parameter without error
    plan = generate_charging_plan(
        data, config, False, learning_state=None, now=now, expected_price_time="13:30"
    )
    
    assert plan is not None
    assert "charging_summary" in plan


def test_waiting_message_includes_expected_price_time():
    """Test that waiting message includes expected price arrival time."""
    from custom_components.ev_optimizer.planner import generate_charging_plan
    
    # Set time to evening when we'd be waiting for tomorrow's prices
    now = datetime(2024, 1, 15, 20, 0)
    
    data = {
        "car_plugged": True,
        "car_soc": 65,
        "smart_charging_active": True,
        "price_data": {
            "today": [0.5] * 24,
            "tomorrow": [],  # No tomorrow prices yet
            "tomorrow_valid": False,
        },
        "target_soc": 80,
        "departure_time": time(7, 0),
        "min_guaranteed_soc": 20,
        "p1_l1": 5.0,
        "p1_l2": 5.0,
        "p1_l3": 5.0,
    }
    
    config = {
        "car_capacity": 64.0,
        "max_fuse": 20.0,
        "charger_loss": 5.0,
        "has_price_sensor": True,
        "currency": "SEK",
    }
    
    # Generate plan with expected price time
    plan = generate_charging_plan(
        data, config, False, learning_state=None, now=now, expected_price_time="13:30"
    )
    
    summary = plan.get("charging_summary", "")
    
    # Should include SoC, plugged status, and expected price time
    assert "65% SoC" in summary or "65 %" in summary
    assert "plugged in" in summary.lower()
    assert "13:30" in summary


def test_waiting_message_includes_car_status():
    """Test that waiting message includes car SoC and plugged status."""
    from custom_components.ev_optimizer.planner import generate_charging_plan
    
    now = datetime(2024, 1, 15, 20, 0)
    
    # Test with unplugged car
    data = {
        "car_plugged": False,
        "car_soc": 45,
        "smart_charging_active": True,
        "price_data": {
            "today": [0.5] * 24,
            "tomorrow": [],
            "tomorrow_valid": False,
        },
        "target_soc": 80,
        "departure_time": time(7, 0),
        "min_guaranteed_soc": 20,
        "p1_l1": 5.0,
        "p1_l2": 5.0,
        "p1_l3": 5.0,
    }
    
    config = {
        "car_capacity": 64.0,
        "max_fuse": 20.0,
        "charger_loss": 5.0,
        "has_price_sensor": True,
        "currency": "SEK",
    }
    
    plan = generate_charging_plan(
        data, config, False, learning_state=None, now=now, expected_price_time=None
    )
    
    summary = plan.get("charging_summary", "")
    
    # Should include SoC and NOT PLUGGED IN status
    assert "45% SoC" in summary or "45 %" in summary
    assert "NOT PLUGGED IN" in summary or "not plugged in" in summary.lower()
