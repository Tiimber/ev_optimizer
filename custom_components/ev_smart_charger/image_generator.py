"""Image generation for EV Smart Charger."""

import logging
import math
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

    # Desired Sizes
    s_header = 26
    s_text = 19
    s_small = 14

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
            if point["charging"] == 1:
                if current_block is None:
                    current_block = {
                        "start": point["time"],
                        "soc_start": point["soc"],
                        "soc_end": point["soc"],
                    }
                current_block["soc_end"] = point["soc"]
                current_block["end"] = point["time"]
            else:
                if current_block:
                    charging_blocks.append(current_block)
                    current_block = None
        if current_block:
            charging_blocks.append(current_block)

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
            line = f"- {start_str} to {end_str} ({int(block['soc_start'])}% -> {int(block['soc_end'])}%)"
            draw.text((40, y), line, font=font_small, fill="black")
            y += 30
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

        for i, point in enumerate(history):
            x0 = margin_left + (i * bar_w_float)
            x1 = margin_left + ((i + 1) * bar_w_float)

            p_norm = (point["price"] - axis_min_p) / price_range
            p_h = p_norm * graph_height
            draw.rectangle(
                [x0, graph_bottom - p_h, x1, graph_bottom], fill="#e0e0e0", outline=None
            )

            if point["charging"] == 1:
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
    else:
        soc_line = f"SoC:   {int(current_soc)}% -> {int(target_soc)}%"

    s_fmt = start_dt.strftime("%d/%m %H:%M")
    e_fmt = end_dt.strftime("%d/%m %H:%M")

    lines = [
        f"Plan:  {s_fmt} -> {e_fmt}",
        soc_line,
        f"Est Cost: {cost_str}",
        f"State: {data.get('current_price_status', 'Unknown')}",
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
    margin_right = 20
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
            [x0, graph_bottom - p_h, x1, graph_bottom], fill="#e0e0e0", outline=None
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
