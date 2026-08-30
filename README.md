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
* **Measured, Not Assumed:** Where you provide an energy or power sensor for the charger, state-of-charge estimation, session totals and efficiency learning all run on what actually flowed rather than on what was commanded. Every such sensor is optional and the system degrades gracefully without them.
* **Plans Sized on Real Performance:** Charging plans are sized from the power this setup actually delivers, which accounts both for your car's onboard charger limit and for the time load balancing spends throttling the charger.
* **Battery-Friendly Scheduling (Optional):** Finish charging close to departure instead of hours before it, at no extra cost.

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
* **Charger Power (Optional):** A sensor reporting what the charger is delivering right now (W or kW).
* **Charger Energy Meter (Optional, recommended):** A cumulative kWh sensor for the charger. This is the single most useful optional sensor you can provide — see [Measuring Delivered Energy](#measuring-delivered-energy).
* **Price Sensor:** Electricity/Nordpool sensor (Optional).
* **Zaptec Controls:**
    * **Switch (Recommended):** The main switch entity for the charger (e.g., `switch.zaptec_charging`).
    * **Limiter:** The entity to set the current limit (Amps).
* **Settings:** Main Fuse Size (A), Charger Efficiency Loss (%), and Currency.
* **Scheduling Preferences (Optional, both default off):**
    * **Finish charging close to departure:** When several price slots cost the same, use the later ones. Costs nothing and reduces the hours the pack spends sitting at a high state of charge.
    * **Charge in one unbroken block:** Charge in a single stretch rather than the cheapest scattered slots. Usually costs more; starts and stops the charger only once.
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

## Adaptive Efficiency Learning

The system learns your charger's real efficiency loss over time, so charging
plans allocate the right amount of time and cost estimates match the bill.

### How It Works

Each measurement runs **between two state-of-charge values the car actually
reported**. Over that window the system knows what it predicted and what the car
says really happened, and the difference gives the true loss directly:

```
loss_true = 100 x (1 - (measured_gain / predicted_gain) x (1 - loss_used/100))
```

That single-shot estimate is noisy, so it is blended into the running value with
a step that shrinks as confidence grows.

* **Why two reported values:** cars report only when they next phone home — 41
  minutes was measured on this author's car — so comparing a live estimate
  against a reading of unknown age measures the reporting delay rather than the
  charger. Anchoring both ends of the window to real readings cancels that delay
  out, whatever it happens to be.
* **Windows that teach nothing are discarded:** anything containing unmeasured
  charge (the commanded-current fallback), a state of charge clamped at the car's
  limit, or less than 3 % of predicted gain, where the car's whole-percent
  reporting dominates the signal.
* **Confidence-based locking:** confidence rises while measurements agree with
  the running value, within a margin that tightens from ±3 % to ±1 %. At 8/10 the
  value locks.
* **Safety buffer:** during the first 10 sessions a 30-minute buffer is added to
  charging plans so an unconverged estimate cannot undershoot your target.

### Configuration

To enable adaptive learning:
1. Go to **Settings > Devices & Services > EV Optimizer > Configure**
2. Navigate to the **Car Configuration** step
3. Set **Car Refresh Interval** to "When target reached (Smart)"
4. The system starts learning on your next charging session

Forced refreshes exist to *produce* readings to anchor measurements against;
the measurement itself happens whenever a new reading lands.

### Monitoring Learning Progress

* **Learned Charger Efficiency** (`sensor.learned_charger_efficiency`): the current learned loss percentage.
  * **Attributes:** `confidence` (0-10), `locked`, `sessions_completed`, `measurements_taken`, `status`, `last_3_measurements` (each showing predicted gain, measured gain, the loss in use at the time, and the loss it implied).
* **Efficiency Learning Confidence** (`sensor.efficiency_learning_confidence`): confidence as an "x/10" rating.

**Sessions and measurements are counted separately.** A session is one plug-in;
a measurement is one usable window. A session can end without producing either,
and a long session can produce several.

### Reset Learning

If you change chargers or want to restart:
1. Use the **Reset Efficiency Learning** button from your dashboard, or
2. Press `button.reset_efficiency_learning`

Learning restarts from your configured charger loss percentage.

### Technical Details

* **Learning range:** -10 % to +30 %. The figure absorbs every source of model
  error — charger loss, a mis-configured pack capacity, mains voltage away from
  230 V — so a small negative correction is meaningful and means the estimator
  under-predicts the car's real gain.
* **Persistence:** learned values survive Home Assistant restarts.
* **Model versioning:** learned values are tied to the estimator that produced
  them. If the estimator changes in a future release, stored values are
  discarded and learning restarts rather than carrying a number that no longer
  measures what its name says. This happened once, at v1.1.25.

> **Fixed in v1.1.25:** the estimator read the charger loss straight from the
> original setup data while the planner used the learned value, so the loop
> adjusted a figure that never fed the quantity it was measuring and could not
> converge. If your learned efficiency sat at 0 % claiming high confidence, that
> was why. Learning restarts automatically on upgrade.

## Load Balancing Details

The system protects your home's electrical system by calculating the maximum safe current available for charging at any moment:

* **Safety Margin:** A 5% buffer is always maintained below your main fuse limit to account for voltage fluctuations.
* **6A Minimum:** The charger requires at least 6A to safely operate. If less is available, charging pauses automatically (overload prevention).
* **Phase Awareness:** If you provide charger current sensors (L1/L2/L3), the system calculates `Available = Main Fuse - (Total Grid Load - Charger Load)`. Without these sensors it falls back to the charger's commanded limiter value, which is amps *per phase*.
* **Real-time Adjustment:** The system listens for immediate updates from your electricity meter (P1) and recalculates available current instantly (with a 2-second debounce), providing protection against overload spikes.

> **Fixed in v1.1.25:** the limiter fallback previously treated that value as a total and divided it across three phases, under-subtracting the charger's own draw. On installations without charger current sensors this made the charger's draw read as house load, producing a start/stop oscillation every update cycle. If you charge without charger current sensors and have seen the charger cycling, this was why.

If charging is prevented due to insufficient current, the system automatically compensates by extending your charging schedule into cheaper price slots, ensuring you still reach your target on time.

## Virtual SoC Estimator

Cars report their state of charge only when they next phone home, which can be
tens of minutes. Between those reports the system walks an estimate forward from
the energy delivered, so decisions do not have to wait for the car.

* **Efficiency Factored In:** The effective charger loss — learned if learning is enabled, otherwise the configured value — is applied to grid energy before it is added to the pack.
* **Reset on Update:** When the car reports a genuinely new value, the estimate is replaced by it.
* **Exposed as `sensor.ev_optimizer_virtual_state_of_charge`,** with the reported value, the current drift between the two, and which measurement source is in use as attributes.

### Measuring Delivered Energy

Energy delivered is established from the best source you have configured, in
this order. Each rung is used only when the ones above it are unavailable:

| Source | Needs | Notes |
| --- | --- | --- |
| Cumulative energy meter | `Charger Energy Meter` | Best. No voltage or phase-count assumption; reports what actually flowed. Handles Wh or kWh and copes with meter resets. |
| Power sensor | `Charger Power` | Integrated over each update interval. |
| Phase currents | `Charger Current L1/L2/L3` | Summed and multiplied by 230 V — correct for single-phase chargers as well as three-phase. |
| Commanded current | nothing | Fallback. Assumes the current we asked for was delivered. |

**The last rung is the one that can drift.** If the car stops accepting charge —
it hit its own limit, or ended the session — nothing measures that, and the
estimate keeps climbing while no energy flows. `binary_sensor.charge_estimate_drift`
turns on when this has been happening for more than five minutes. Configuring
any of the sources above it makes that impossible, which is why the energy meter
is worth adding if you have one.

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
* **Reset Efficiency Learning** (`button`): Resets the adaptive learning system to restart from your configured charger loss percentage.

### Sensors (Visualization)
* **Charging Schedule** (`sensor`): Contains the complex data for the graph (prices, charging windows) and a text summary attribute `charging_summary`.
* **Max Safe Current** (`sensor`): The dynamic amperage limit calculated to protect your main fuse.
* **Charger Logic Status** (`sensor`): Shows current state (Disconnected, Waiting, Charging).
* **Price Logic** (`sensor`): Indicates if current electricity price is considered Cheap/Expensive relative to the day's average.
* **Learned Charger Efficiency** (`sensor`): Shows the automatically learned efficiency loss percentage with confidence level and learning status.
* **Efficiency Learning Confidence** (`sensor`): Displays learning confidence as "x/10" rating.
* **Virtual State of Charge** (`sensor`): The state of charge every decision is actually made on, between the car's own reports.
* **Planned Charge Power** (`sensor`): The power figure charging plans are sized on, and where it came from (measured, or derived from your fuse rating).
* **Charging Savings** (`sensor`): What the schedule saved compared with simply charging on plug-in, with both figures costed on identical terms.
* **Planned Departure** (`sensor`, timestamp): When the car is expected to be needed, with `source` naming whether that came from the calendar, an override, or your standard time. Useful as a trigger for preconditioning or climate automations.

### Diagnostics

* **Manual Override Active** (`binary_sensor`): Whether the next session's target came from you rather than from the planner. Previously this could only be inferred from summary text.
* **Car Limit Drift** (`binary_sensor`, problem): The car's reported charge limit disagrees with the one we applied. Setting the limit is fire-and-forget, so a silent disagreement lets the estimate run above the car's real ceiling.
* **Charge Estimate Drift** (`binary_sensor`, problem): The charger reports no output while we believe it is charging. See [Measuring Delivered Energy](#measuring-delivered-energy).

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
