# EV Optimizer for Home Assistant

***Optimize your EV charging costs with smart scheduling and load balancing***

**EV Optimizer** is a custom Home Assistant integration designed to optimize electric vehicle charging based on real-time electricity prices (eg. Nordpool), household load balancing (P1 Meter), and user scheduling. It also allows you to charge your car to different percentages based on if the electricity price is very low of high.

This is written to fit my need, which is a very specific combination, but I've tried to cover a more generic use case and I do believe you could create your own switches and sensors with custom templates and automations to make practically any brands work. My specific combination is Nordpool for electricity spot price, a P1ib device hooked into my electric meters' HAN-port of my electric meter, Zaptec Go as charger for my Hyundai Kona, connected through the Hyundai / Kia Connect Custom Integration.

I also have a Local Calendar integration, where I in advance have set my specific, varying, but on a recurring schedule with ten minute events of each morning, which I use for automatically starting climate just before when I leave home. I have made this calendar compatible for knowing the target time of the charging window as well. If people are interested, I could bake in my Climate control code into this integration as well, or perhaps make that available as another custom integration.

**Note!** *Much of the code (and tests) was written by AI (Gemini and Copilot), and manually tested and heavily inspected and questioned by me in several iterations. I am a programmer in my work, but Python and Home Assistant specifics aren't my specialities.*

**Sample dashboard card**

[![Sample Dashboard Card](./dashboard_card_typical_small.png)](./dashboard_card_typical_full.png)

*More Dashboard code samples, with YAML code, can be found through the link in the bottom of this file.*

## Features

* **Smart Scheduling:** Automatically plans charging during the cheapest hours between now and your departure time.
* **Load Balancing:** Dynamically calculates the maximum safe current to prevent tripping your main house fuse (based on real-time P1ib meter readings).
* **Cost Estimation:** Calculates the total session cost including Spot Price, Grid Fees, and VAT.
* **Calendar Integration:** Reads your local calendar for events to set custom departure times and target charge levels automatically.
  * In my case, I have varying schedule that repeats every two weeks, one pattern the first week and another the next week.
* **Manual Overrides:** Easily override the next charging session's target percentage or departure time without affecting your standard daily schedule.
* **Maintenance Mode:** Keeps the charger active (if price allows) after reaching the target to support battery balancing or climate control.
* **Overload Prevention with Automatic Compensation:** If your house fuse is fully loaded and the charger cannot be allocated 6A minimum, charging pauses to protect your home. The system automatically tracks these prevented minutes and extends your charging plan with extra time during the cheapest available hours to ensure you still reach your target.

## Prerequisites

Before installing, ensure you have the following:

1.  **P1ib/Grid Meter:** Sensors reporting current (Amps) for Phase 1, 2, and 3.
2.  **Electricity Price Sensor (Optional):** A sensor providing electricity prices with `today` and `tomorrow` attributes per quarter of an hour (e.g., via the [Nordpool custom component](https://github.com/custom-components/nordpool)). If omitted, the integration will perform Load Balancing only (charging immediately).
3.  **ApexCharts Card (Optional):** Required for the recommended visualization graph. Install via HACS (Frontend > ApexCharts Card).

## Installation

### Method 1: HACS (Recommended)

1.  Open **HACS** in Home Assistant.
2.  Go to **Integrations** > **Top Right Menu** > **Custom repositories**.
3.  Add `Tiimber/ev_optimizer` as an **Integration**.
4.  Click **Download**.
5.  Restart Home Assistant.

### Method 2: Manual

1.  Copy the `custom_components/ev_optimizer` folder into your Home Assistant `config/custom_components/` directory.
2.  Restart Home Assistant.

## Configuration

1.  Go to **Settings > Devices & Services > Add Integration**.
2.  Search for **EV Optimizer** and follow the configuration steps.

## Configuration Parameters

During setup, you will map your existing Home Assistant entities:

* **Car Sensors:** SoC (State of Charge) and Plugged-in status.
* **Car Limit (Optional):** Entity to set the limit on the car itself (e.g. `number.kona_charge_limit`).
* **Grid Sensors:** Current (Amps) for L1, L2, and L3 from your P1ib meter.
* **Charger Current:** Current (Amps) for L1/L2/L3 *of the charger itself*. If provided, the logic becomes `Available = Main Fuse - (Grid Total - Charger Load)`. This prevents oscillation loops.
* **Price Sensor:** Electricity/Nordpool sensor (Optional).
* **Zaptec Controls:**
    * **Switch (Recommended):** The main switch entity for the charger (e.g., `switch.zaptec_charging`).
    * **Limiter:** The entity to set the current limit (Amps).
* **Settings:** Main Fuse Size (A), Charger Efficiency Loss (%), and Currency.
* **Calendar (Optional):** A Local Calendar entity to read for trip planning, or advanced recurring travel.

## Advanced Configuration Parameters

The following parameters are optional but provide finer control:

* **Car Refresh Logic (Optional):** Force periodic updates of car state from the vehicle API.
    * **Refresh Action:** Service to call (e.g., `kia_uvo.force_update`)
    * **Refresh Interval:** How often to refresh (Never, 30 min, 1 hour, 2 hours, etc.)
* **Zaptec Resume/Stop (Alternative):** If you don't have a Switch entity, use separate Resume and Stop buttons instead.
* **Car Service Call (Alternative):** If your car supports a charging-limit service instead of an entity.
* **Smart Price Tiers:** Set different charging targets based on electricity prices. (Settable through exposed entities)
    * **Price Limit 1 & Target SoC 1:** Very cheap price threshold (e.g., charge to 90% if ≤ 0.10).
    * **Price Limit 2 & Target SoC 2:** Expensive price threshold (e.g., charge only to 70% if ≥ 3.00).

## How to Use

### 1. Smart Mode (Default)
Simply plug in your car. The integration will:
* Calculate the cheapest hours to reach your **Standard Target SoC** by your **Standard Departure Time**.
* Start/Stop the charger automatically.
* Adjust current limits to protect your house fuses.
* If electricity price is low enough, you can have it automatically charge to a higher percentage. Likewise for a very high price, you can set a lower value.

### 2. Manual Override
If you need to leave earlier or charge more for a specific trip:
* Adjust the **"Next Session"** slider or time.
* The system immediately switches to **Override Mode**, strictly following your new settings.
* **Auto-Reset:** When you unplug the car, these overrides automatically reset to your standard defaults, returning the system to Smart Mode for the next charging session.
* **Manual Reset:** You can also reset overrides anytime by pressing the **"Clear Manual Override"** button, which is available to be added to the dashboard (see samples linked at the bottom of this document).

### 3. Calendar Automation
Create an event in your configured calendar. The integration looks for events within the next 24 hours.
* **Departure Time:** Taken from the event Start Time.
* **Target SoC:** To set a custom target, include a percentage in the event title or description (e.g., **"Trip to Cabin 90%"**).

#### Calendar Event Format

The system scans your calendar for events within the next 24 hours. To set a custom charging target, include a percentage in the event title or description:

**Examples:**
* `Trip to Cabin 100%` — Charges to 100% by the event start time
* `Work Conference 80%` — Targets 80%
* `Airport 60%` — Targets 60%

The percentage should be between 10% and 100%. If multiple percentages are found, the first one is used. The event's start time becomes the charging deadline.

### 4. Maintenance Mode
After your car reaches the target SoC, the system can keep the charger active at 0A (no current) to support:
* **Battery Balancing:** Some vehicles use low-current charging to balance cell voltages for battery longevity.
* **Climate Control:** Charging keeps the vehicle awake and powered, allowing preconditioning or climate systems to run.
* **Smart Pricing:** Even in maintenance mode, charging only remains active if the current electricity price is below your configured threshold.

You can disable maintenance mode entirely by setting your target SoC lower, or let it run passively while you're at home.

## Load Balancing Details

The system protects your home's electrical system by calculating the maximum safe current available for charging at any moment:

* **Safety Margin:** A 5% buffer is always maintained below your main fuse limit to account for voltage fluctuations.
* **6A Minimum:** The charger requires at least 6A to safely operate. If less is available, charging pauses automatically (overload prevention).
* **Phase Awareness:** If you provide charger current sensors (L1/L2/L3), the system calculates `Available = Main Fuse - (Total Grid Load - Charger Load)`. Without these sensors, it falls back to using the Zaptec limiter value to estimate current usage.
* **Real-time Adjustment:** The system listens for immediate updates from your electricity meter (P1) and recalculates available current instantly (with a 2-second debounce), providing protection against overload spikes.

If charging is prevented due to insufficient current, the system automatically compensates by extending your charging schedule into cheaper price slots, ensuring you still reach your target on time.

## Virtual SoC Estimator

Between actual car state readings, the system estimates your car's State of Charge based on charging activity:

* **Charging Current & Time:** If the charger is active, the system calculates estimated energy delivered using the applied current (Amps) and duration.
* **Efficiency Factored In:** The charger loss percentage you configured is accounted for in the calculation.
* **Accuracy:** These estimates help the system make smarter decisions about when to start/stop charging without waiting for the next car API update (which can take minutes).
* **Reset on Update:** When a real SoC reading is received from the car, the virtual estimate is replaced with the actual value to correct any drift.

This allows the system to react faster to changing conditions and provides more accurate cost projections for your charging session.

## Entities Explained

This integration creates several entities to help you control and visualize the charging process.

### Controls (Dashboard)
* **Smart Charging Enabled** (`switch`): Master switch. If OFF, the car charges immediately regardless of price.
* **Standard Target SoC** (`number`): Your default daily charge target (e.g., 80%).
* **Next Session Target SoC** (`number`): **Override.** Changing this slider immediately sets a strict target for the *current* session only.
* **Standard Departure Time** (`time`): Default time you leave every morning (e.g., 07:00).
* **Next Session Departure** (`time`): **Override.** Sets a specific departure time for the next trip. Resets to standard after unplugging.
* **Extra Cost per kWh** (`number`): Set your grid fees/transfer costs here to get accurate price summaries. (If not already included in your Electricity Price sensor).
* **VAT Percentage** (`number`): Set your local VAT (e.g., 25) to be added to the spot price.  (If not already included in your Electricity Price sensor).
* **Clear Manual Override** (`button`): Reverts any manual slider/time changes and returns to "Smart Mode" (calculating targets based on price/calendar).
* **Refresh Charging Plan** (`button`): Forces a recalculation of the schedule immediately.

### Sensors (Visualization)
* **Charging Schedule** (`sensor`): Contains the complex data for the graph (prices, charging windows) and a text summary attribute `charging_summary`.
* **Max Safe Current** (`sensor`): The dynamic amperage limit calculated to protect your main fuse.
* **Charger Logic Status** (`sensor`): Shows current state (Disconnected, Waiting, Charging).
* **Price Logic** (`sensor`): Indicates if current electricity price is considered Cheap/Expensive relative to the day's average.

### Charging Report and Charging Plan

These were made "for fun", but also serve some purpost. They were made to be printed on a thermal printer.

**Charging Plan** provides a real-time visual forecast of when charging will occur and at what cost:
* Updates when manually triggered.
* Shows electricity price trends, selected charging windows, and estimated SoC progression.
* Includes total estimated cost for the session with spot price, grid fees, and VAT factored in.
* Helps you understand why charging is (or isn't) happening at any given moment.

**Charging Report** captures the details of each completed charging session:
* Generated automatically when the car is unplugged.
* Displays session duration, actual SoC gain, cost breakdown, and a historical graph of prices and charging activity.
* Includes the total time prevented by overload protection (if any occurred), so you can see how the system compensated for grid constraints.

Both are available as camera entities for easy integration into your Home Assistant dashboard see below for examples.

## Price Sensor Data Format

If using a price sensor (e.g., Nordpool), the integration expects it to have the following attributes:

* **`today`:** A list of electricity prices for today, either:
    * 96 entries (quarter-hourly: every 15 minutes) — e.g., 96 prices for a full 24-hour day
    * 24 entries (hourly: every hour) — e.g., 24 prices for a full 24-hour day
    * Edge cases: 92–100 entries for 15-min, or 23–25 entries for hourly are also accepted
* **`tomorrow`:** A list of prices for tomorrow (same format as `today`). Optional, but recommended for better planning.
* **`tomorrow_valid`:** A boolean attribute indicating whether tomorrow's prices are available and reliable.
* **Price units:** Prices should be in your local currency per kWh (e.g., SEK/kWh, EUR/kWh).

The integration automatically detects the interval granularity (15-min vs. hourly) based on list length and adapts slot durations accordingly. If your price sensor has a different attribute name, you may need to create a template sensor as an intermediary.

## Dashboard Configuration

To visualize the plan with a graph (as seen in image above), use the **ApexCharts Card** (available via HACS).

[All prepared Dashboard Card samples can be found here](./dashboard_cards.md)

## Debugging & Troubleshooting

If the integration is not behaving as expected (e.g., charging starts immediately when it shouldn't), the integration includes comprehensive debugging tools:

### Quick Debugging Steps:

1. **Enable debug logging** in `configuration.yaml`:
   ```yaml
   logger:
     default: info
     logs:
       custom_components.ev_optimizer: debug
   ```

2. **Use the "Dump Debug State" button** (or service `ev_optimizer.dump_debug_state`)
   - This outputs a complete JSON snapshot of all configuration, sensor data, and price data to your logs
   - Copy the JSON and share it when reporting issues

3. **Check the detailed logs** which now explain every decision step:
   - Why a particular target SOC was chosen
   - Which price slots were selected and why
   - Whether the system is waiting for more price data
   - Energy calculations and timing

For complete debugging guide and examples, see [DEBUGGING.md](./DEBUGGING.md).
