# EV Smart Charger & Load Balancer for Home Assistant

**EV Smart Charger** is a custom Home Assistant integration designed to optimize electric vehicle charging based on real-time electricity prices (Nordpool), household load balancing (P1 Meter), and user scheduling.

It specifically targets users with **Zaptec** chargers (or similar controllable entities) and **3-phase** household power metering.

**Note!** *Much of the code was written by AI, and just tested and heavily inspected and questioned by me in several iterations. I am a programmer in my work, but Python and Home Assistant specifics aren't my specialities.*

*This is written to fit my need, which is a very specific combination, but I've tried to cover a more generic use case. My combination is Nordpool spot price, a P1ib device hooked into my electric meters' HAN-port, Zaptec Go as charger for my Hyundai Kona, connected through the Hyundai / Kia Connect Custom Integration.*

*I also have a Local Calendar integration, where I in advance have set my specific, varying, but on a recurring schedule with ten minute events of each morning, which I use for automatically starting climate just before when I leave home. I have made this calendar compatible for the available charging window as well. If people are interested, I could bake that into this integration as well, or perhaps make that available as another custom integration.*

**Sample dashboard card**

[![Full Dashboard Card](./dashboard_card_small.png)](./dashboard_card_full.png)

## Features

* **Smart Scheduling:** Automatically plans charging during the cheapest hours between now and your departure time.
* **Load Balancing:** Dynamically calculates the maximum safe current to prevent tripping your main house fuse (based on real-time P1ib meter readings).
* **Calendar Integration:** Reads your local calendar for events to set custom departure times and target charge levels automatically.
* **Manual Overrides:** Easily override the next charging session's target percentage or departure time without affecting your standard daily schedule.
* **Maintenance Mode:** Keeps the charger active (if price allows) after reaching the target to support battery balancing or climate control.

## Prerequisites

Before installing, ensure you have the following:

1.  **Electricity Price Sensor (Optional):** A sensor providing electricity prices with `today` and `tomorrow` attributes per quarter of an hour (e.g., via the [Nordpool custom component](https://github.com/custom-components/nordpool)). If omitted, the integration will perform Load Balancing only (charging immediately).
2.  **ApexCharts Card:** Required for the recommended visualization graph. Install via HACS (Frontend > ApexCharts Card).
3.  **P1ib/Grid Meter:** Sensors reporting current (Amps) for Phase 1, 2, and 3.

## Installation

### Method 1: HACS (Recommended)

1.  Open **HACS** in Home Assistant.
2.  Go to **Integrations** > **Top Right Menu** > **Custom repositories**.
3.  Add `Tiimber/ev_smart_charger` as an **Integration**.
4.  Click **Download**.
5.  Restart Home Assistant.

### Method 2: Manual

1.  Copy the `ev_smart_charger` folder into your Home Assistant `config/custom_components/` directory.
2.  Restart Home Assistant.

## Configuration

1.  Go to **Settings > Devices & Services > Add Integration**.
2.  Search for **EV Smart Charger** and follow the configuration steps.

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
* **Reset:** When you unplug the car, these overrides automatically reset to your standard defaults.

### 3. Calendar Automation
Create an event in your configured calendar. The integration looks for events within the next 24 hours.
* **Departure Time:** Taken from the event Start Time.
* **Target SoC:** To set a custom target, include a percentage in the event title or description (e.g., **"Trip to Cabin 90%"**).

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

## Dashboard Configuration

To visualize the plan with a graph (as seen in image above), use the **ApexCharts Card** (available via HACS).

[All prepared Dashboard Card samples can be found here](./dashboard_cards.md)
