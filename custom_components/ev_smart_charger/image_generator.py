"""Image generation for EV Smart Charger."""

import logging
import math
import re
import os
from datetime import datetime

# Try to import PIL, log warning if missing
try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)


def _load_fonts():
    """Helper to load standard fonts with fallbacks."""
    if not PIL_AVAILABLE:
        return None, None, None

    # Get component directory for bundled fonts
    component_dir = os.path.dirname(__file__)

    # Paths to try for TrueType fonts
    font_candidates = [
        # 1. Bundled Font
        os.path.join(component_dir, "DejaVuSans.ttf"),
        # 2. System Paths
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",  # Alpine default
        "arial.ttf",
    ]

    font_header = None
    font_text = None
    font_small = None

    # Desired Sizes (increased by 4pt for better readability on thermal printer)
    s_header = 30
    s_text = 23
    s_small = 18

    found_path = None

    for path in font_candidates:
        if os.path.exists(path):
            found_path = path
            break
        try:
            ImageFont.truetype(path, s_header)
            found_path = path
            break
        except OSError:
            continue

    # Fallback search
    if not found_path:
        search_dirs = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            "/root/.local/share/fonts",
        ]
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for root, _, files in os.walk(search_dir):
                for file in files:
                    if file.lower().endswith(".ttf"):
                        if "bold" in file.lower() and "sans" in file.lower():
                            found_path = os.path.join(root, file)
                            break
                        if "bold" in file.lower() and not found_path:
                            found_path = os.path.join(root, file)
                if found_path:
                    break
            if found_path:
                break

    if found_path:
        try:
            font_header = ImageFont.truetype(found_path, s_header)
            font_text = ImageFont.truetype(found_path, s_text)
            font_small = ImageFont.truetype(found_path, s_small)
        except OSError:
            font_header = None

    if not font_header:
        font_header = ImageFont.load_default()
        font_text = ImageFont.load_default()
        font_small = ImageFont.load_default()

    return font_header, font_text, font_small


def generate_report_image(report: dict, file_path: str):
    """Generate a PNG image for thermal printers (Last Session)."""
    if not PIL_AVAILABLE:
        _LOGGER.warning("PIL (Pillow) not found. Cannot generate image.")
        return

    width = 576
    bg_color = "white"
    font_header, font_text, font_small = _load_fonts()

    history = report.get("graph_data", [])
    charging_blocks = []
    if history:
        current_block = None
        for i, point in enumerate(history):
            # Track SoC sensor refreshes within blocks
            soc_refresh = point.get("soc_sensor_refresh", False)
            
            if point["charging"] == 1:
                if current_block is None:
                    current_block = {
                        "start": point["time"],
                        "soc_start": point["soc"],
                        "soc_end": point["soc"],
                        "soc_refreshes": [],
                    }
                if soc_refresh:
                    current_block["soc_refreshes"].append(point["time"])
                current_block["soc_end"] = point["soc"]
                current_block["end"] = point["time"]
            else:
                if current_block:
                    charging_blocks.append(current_block)
                    current_block = None
        if current_block:
            charging_blocks.append(current_block)

    # Merge blocks with gaps ≤ 2 minutes
    merged_blocks = []
    for block in charging_blocks:
        if not merged_blocks:
            merged_blocks.append(block)
        else:
            last = merged_blocks[-1]
            last_end = datetime.fromisoformat(last["end"])
            curr_start = datetime.fromisoformat(block["start"])
            gap = (curr_start - last_end).total_seconds() / 60.0
            if gap <= 2.0:
                # Merge: extend last block
                last["end"] = block["end"]
                last["soc_end"] = block["soc_end"]
                last["soc_refreshes"].extend(block["soc_refreshes"])
            else:
                merged_blocks.append(block)
    charging_blocks = merged_blocks

    text_section_height = 600 + (len(charging_blocks) * 35)
    height = text_section_height + 400

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # --- HEADER ---
    y = 30
    draw.text(
        (width // 2, y),
        "EV Charging Report",
        font=font_header,
        fill="black",
        anchor="mt",
    )
    y += 70

    lines = [
        f"Start: {report['start_time'][:16].replace('T', ' ')}",
        f"End:   {report['end_time'][:16].replace('T', ' ')}",
        f"Power: {report['added_kwh']} kWh",
        f"Cost:  {report['total_cost']} {report['currency']}",
        f"SoC:   {int(report['start_soc'])}% -> {int(report['end_soc'])}%",
    ]
    
    # Add overload prevention minutes if available
    overload_mins = report.get("overload_prevention_minutes", 0.0)
    if overload_mins > 0:
        lines.append(f"Prevented: {int(overload_mins)} min (overload)")

    for line in lines:
        draw.text((30, y), line, font=font_text, fill="black")
        y += 35

    y += 15
    draw.line([(10, y), (width - 10, y)], fill="black", width=3)
    y += 30

    # --- LOG ---
    if charging_blocks:
        draw.text((30, y), "Charging Activity:", font=font_text, fill="black")
        y += 40
        for block in charging_blocks:
            start_dt = datetime.fromisoformat(block["start"])
            end_dt = datetime.fromisoformat(block["end"])
            start_str = start_dt.strftime("%H:%M")
            end_str = end_dt.strftime("%H:%M")
            refresh_note = " *" if block.get("soc_refreshes") else ""
            line = f"- {start_str} to {end_str} ({int(block['soc_start'])}% -> {int(block['soc_end'])}%){refresh_note}"
            draw.text((40, y), line, font=font_small, fill="black")
            y += 30
        # Add legend if any blocks have sensor refresh
        if any(block.get("soc_refreshes") for block in charging_blocks):
            draw.text((40, y), "* SoC refreshed from sensor", font=font_small, fill="gray")
            y += 25
    else:
        draw.text((30, y), "No charging recorded.", font=font_text, fill="black")
        y += 40

    y += 20

    # --- GRAPH ---
    if history:
        graph_top = y
        graph_height = 250
        graph_bottom = graph_top + graph_height

        margin_left = 60
        margin_right = 60
        graph_draw_width = width - margin_left - margin_right

        prices = [p["price"] for p in history]
        min_p = min(prices) if prices else 0
        max_p = max(prices) if prices else 1
        axis_min_p = math.floor(min_p * 2) / 2
        axis_max_p = math.ceil(max_p * 2) / 2
        if axis_max_p == axis_min_p:
            axis_max_p += 0.5
        price_range = axis_max_p - axis_min_p

        count = len(history)
        bar_w_float = graph_draw_width / max(1, count)

        # Group charging bars with gaps ≤2 min to draw as continuous blocks
        charging_bar_ranges = []
        current_range = None
        for i, point in enumerate(history):
            if point["charging"] == 1:
                if current_range is None:
                    current_range = {"start_idx": i, "end_idx": i, "last_time": point["time"]}
                else:
                    last_t = datetime.fromisoformat(current_range["last_time"])
                    curr_t = datetime.fromisoformat(point["time"])
                    gap = (curr_t - last_t).total_seconds() / 60.0
                    if gap <= 2.0:
                        current_range["end_idx"] = i
                        current_range["last_time"] = point["time"]
                    else:
                        charging_bar_ranges.append(current_range)
                        current_range = {"start_idx": i, "end_idx": i, "last_time": point["time"]}
            else:
                if current_range:
                    charging_bar_ranges.append(current_range)
                    current_range = None
        if current_range:
            charging_bar_ranges.append(current_range)

        for i, point in enumerate(history):
            x0 = margin_left + (i * bar_w_float)
            x1 = margin_left + ((i + 1) * bar_w_float)

            p_norm = (point["price"] - axis_min_p) / price_range
            p_h = p_norm * graph_height
            draw.rectangle(
                [x0, graph_bottom - p_h, x1, graph_bottom], fill="#808080", outline=None
            )

        # Draw merged charging bar ranges
        for bar_range in charging_bar_ranges:
            x0 = margin_left + (bar_range["start_idx"] * bar_w_float)
            x1 = margin_left + ((bar_range["end_idx"] + 1) * bar_w_float)
            draw.rectangle(
                [x0, graph_bottom - 20, x1, graph_bottom],
                fill="black",
                outline=None,
            )

        # Axes drawing...
        draw.line(
            [(margin_left, graph_top), (margin_left, graph_bottom)],
            fill="black",
            width=2,
        )
        curr_mark = axis_min_p
        while curr_mark <= axis_max_p + 0.01:
            norm = (curr_mark - axis_min_p) / price_range
            mark_y = graph_bottom - (norm * graph_height)
            draw.line(
                [(margin_left - 5, mark_y), (margin_left, mark_y)],
                fill="black",
                width=1,
            )
            label = f"{curr_mark:.1f}"
            draw.text(
                (margin_left - 45, mark_y - 7), label, font=font_small, fill="black"
            )
            curr_mark += 0.5

        draw.line(
            [(width - margin_right, graph_top), (width - margin_right, graph_bottom)],
            fill="black",
            width=2,
        )
        for soc_mark in [0, 20, 40, 60, 80, 100]:
            norm = soc_mark / 100.0
            mark_y = graph_bottom - (norm * graph_height)
            draw.line(
                [(width - margin_right, mark_y), (width - margin_right + 5, mark_y)],
                fill="black",
                width=1,
            )
            label = f"{soc_mark}%"
            draw.text(
                (width - margin_right + 8, mark_y - 7),
                label,
                font=font_small,
                fill="black",
            )

        points = []
        for i, point in enumerate(history):
            x = margin_left + (i * bar_w_float) + (bar_w_float / 2)
            soc_norm = point["soc"] / 100.0
            y = graph_bottom - (soc_norm * graph_height)
            points.append((x, y))

        if len(points) > 1:
            draw.line(points, fill="black", width=2)

        try:
            start_dt = datetime.fromisoformat(history[0]["time"])
            start_str = start_dt.strftime("%H:%M")
            draw.text(
                (margin_left, graph_bottom + 15),
                start_str,
                font=font_small,
                fill="black",
            )

            end_dt = datetime.fromisoformat(history[-1]["time"])
            end_str = end_dt.strftime("%H:%M")
            try:
                w = draw.textlength(end_str, font=font_small)
                draw.text(
                    (width - margin_right - w, graph_bottom + 15),
                    end_str,
                    font=font_small,
                    fill="black",
                )
            except AttributeError:
                draw.text(
                    (width - margin_right - 50, graph_bottom + 15),
                    end_str,
                    font=font_small,
                    fill="black",
                )
        except Exception:
            pass

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    img.save(file_path)
    _LOGGER.info(f"Saved session image to {file_path}")


def generate_plan_image(data: dict, file_path: str):
    """Generate a PNG image for the future charging plan."""
    if not PIL_AVAILABLE:
        return

    width = 576
    bg_color = "white"
    font_header, font_text, font_small = _load_fonts()

    schedule = data.get("charging_schedule", [])
    if not schedule:
        return
    
    # Filter schedule to only show up to departure time
    departure_time_str = data.get("departure_time")
    if departure_time_str:
        departure_dt = datetime.fromisoformat(departure_time_str)
        schedule = [s for s in schedule if datetime.fromisoformat(s["start"]) <= departure_dt]
    
    valid_slots = [s for s in schedule if s["price"] is not None]
    if not valid_slots:
        return

    height = 650
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    y = 30
    draw.text(
        (width // 2, y), "Charging Plan", font=font_header, fill="black", anchor="mt"
    )
    y += 80
    summary_text = data.get("charging_summary", "")
    cost_match = re.search(r"Total Estimated Cost:\*\* ([\d\.]+) (\w+)", summary_text)
    cost_str = f"{cost_match.group(1)} {cost_match.group(2)}" if cost_match else "N/A"

    start_time = valid_slots[0]["start"]
    end_time = valid_slots[-1]["end"]
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)
    current_soc = data.get("car_soc", 0)
    target_soc = data.get("planned_target_soc", 0)

    if int(current_soc) >= int(target_soc):
        soc_line = f"SoC:   {int(current_soc)}% (Target Reached)"
        cost_str = "0.00 (No charging needed)"
    else:
        soc_line = f"SoC:   {int(current_soc)}% -> {int(target_soc)}%"

    s_fmt = start_dt.strftime("%d/%m %H:%M")
    e_fmt = end_dt.strftime("%d/%m %H:%M")
    
    # Calculate average price per kWh from schedule slots
    prices = [s["price"] for s in valid_slots if s["price"] is not None]
    avg_price = sum(prices) / len(prices) if prices else 0.0

    lines = [
        f"Plan:  {s_fmt} -> {e_fmt}",
        soc_line,
        f"Est Cost: {cost_str}",
        f"Avg Price: {avg_price:.2f} per kWh",
    ]

    for line in lines:
        draw.text((30, y), line, font=font_text, fill="black")
        y += 35
    y += 20
    draw.line([(10, y), (width - 10, y)], fill="black", width=3)
    y += 30

    graph_top = y
    graph_height = 250
    graph_bottom = graph_top + graph_height
    margin_left = 60
    margin_right = 60
    graph_draw_width = width - margin_left - margin_right

    prices = [s["price"] for s in valid_slots]
    min_p = min(prices)
    max_p = max(prices)
    axis_min_p = math.floor(min_p * 2) / 2
    axis_max_p = math.ceil(max_p * 2) / 2
    if axis_max_p == axis_min_p:
        axis_max_p += 0.5
    price_range = axis_max_p - axis_min_p

    count = len(valid_slots)
    bar_w_float = graph_draw_width / max(1, count)

    for i, slot in enumerate(valid_slots):
        x0 = margin_left + (i * bar_w_float)
        x1 = margin_left + ((i + 1) * bar_w_float)
        p_norm = (slot["price"] - axis_min_p) / price_range
        p_h = p_norm * graph_height
        draw.rectangle(
            [x0, graph_bottom - p_h, x1, graph_bottom], fill="#808080", outline=None
        )
        if slot["active"]:
            draw.rectangle(
                [x0, graph_bottom - 20, x1, graph_bottom], fill="black", outline=None
            )

    draw.line(
        [(margin_left, graph_top), (margin_left, graph_bottom)], fill="black", width=2
    )
    curr_mark = axis_min_p
    while curr_mark <= axis_max_p + 0.01:
        norm = (curr_mark - axis_min_p) / price_range
        mark_y = graph_bottom - (norm * graph_height)
        draw.line(
            [(margin_left - 5, mark_y), (margin_left, mark_y)], fill="black", width=1
        )
        label = f"{curr_mark:.1f}"
        draw.text((margin_left - 55, mark_y - 10), label, font=font_small, fill="black")
        curr_mark += 0.5

    # Draw SoC (State of Charge) line on right axis
    draw.line(
        [(width - margin_right, graph_top), (width - margin_right, graph_bottom)],
        fill="black",
        width=2,
    )
    for soc_mark in [0, 20, 40, 60, 80, 100]:
        norm = soc_mark / 100.0
        mark_y = graph_bottom - (norm * graph_height)
        draw.line(
            [(width - margin_right, mark_y), (width - margin_right + 5, mark_y)],
            fill="black",
            width=1,
        )
        label = f"{soc_mark}%"
        draw.text(
            (width - margin_right + 8, mark_y - 7),
            label,
            font=font_small,
            fill="black",
        )

    # Estimate SoC progression for the charging plan
    # Assume linear increase during active charging from current to target SoC
    current_soc = data.get("car_soc", 0)
    target_soc = data.get("planned_target_soc", 80)
    active_count = sum(1 for s in valid_slots if s.get("active"))
    
    soc_points = []
    for i, slot in enumerate(valid_slots):
        # If target already reached, keep SoC flat at current level
        if int(current_soc) >= int(target_soc):
            estimated_soc = current_soc
        elif active_count > 0:
            # Linear interpolation from current to target based on active slots
            progress = sum(1 for s in valid_slots[:i+1] if s.get("active")) / active_count
            estimated_soc = current_soc + (target_soc - current_soc) * progress
        else:
            # No charging, SoC stays constant
            estimated_soc = current_soc
        
        x = margin_left + (i * bar_w_float) + (bar_w_float / 2)
        soc_norm = min(estimated_soc, 100.0) / 100.0
        y = graph_bottom - (soc_norm * graph_height)
        soc_points.append((x, y))
    
    if len(soc_points) > 1:
        draw.line(soc_points, fill="black", width=2)

    draw.text(
        (margin_left, graph_bottom + 15),
        start_dt.strftime("%H:%M"),
        font=font_small,
        fill="black",
    )
    end_str = end_dt.strftime("%H:%M")
    try:
        w = draw.textlength(end_str, font=font_small)
        draw.text(
            (width - margin_right - w, graph_bottom + 15),
            end_str,
            font=font_small,
            fill="black",
        )
    except AttributeError:
        draw.text(
            (width - margin_right - 50, graph_bottom + 15),
            end_str,
            font=font_small,
            fill="black",
        )

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    img.save(file_path)
    _LOGGER.info(f"Saved plan image to {file_path}")
