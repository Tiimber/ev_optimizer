# EV Optimizer Debug Snapshot Card

Example Lovelace card configuration for debugging charging issues using the snapshot system.

## Quick Debug Card

```yaml
type: vertical-stack
cards:
  - type: markdown
    content: |
      ## 🔍 Debug Snapshots
      Capture and replay charging scenarios to understand why the car didn't charge.
      
      **Snapshots are captured automatically every hour when car is plugged in.**
      
  - type: entities
    title: Manual Snapshot Controls
    entities:
      - entity: button.ev_optimizer_capture_debug_snapshot
        name: Capture Snapshot Now
      - entity: button.ev_optimizer_export_debug_snapshots
        name: Export All Snapshots (JSON)
      
  - type: markdown
    content: |
      ### 📊 Replay Last Night
      To investigate why charging didn't work last night:
      
      1. Click "Call Service" below
      2. Enter start time: `2026-03-08T18:00:00` (yesterday evening)
      3. Enter end time: `2026-03-09T08:00:00` (this morning)
      4. Choose output format: `html`
      5. Click "Call Service"
      6. Check www/ev_optimizer_debug_*.html in your browser at `/local/ev_optimizer_debug_*.html`
      
  - type: button
    name: Replay Last Night (Service)
    icon: mdi:replay
    tap_action:
      action: call-service
      service: ev_optimizer.replay_scenario
      service_data:
        start_time: "2026-03-08T18:00:00"
        end_time: "2026-03-09T08:00:00"
        output_format: html
```

## Advanced Debug Card with Service Calls

```yaml
type: vertical-stack
cards:
  - type: markdown
    content: |
      ## 🐛 Advanced Debugging
      
  - type: entities
    title: Snapshot Actions
    entities:
      - entity: button.ev_optimizer_capture_debug_snapshot
      - entity: button.ev_optimizer_export_debug_snapshots
      - entity: button.ev_optimizer_dump_debug_state
        
  - type: custom:service-call
    service: ev_optimizer.replay_scenario
    title: Replay Scenario
    fields:
      - name: start_time
        label: Start Time (ISO)
        type: text
        default: "2026-03-08T18:00:00"
      - name: end_time
        label: End Time (ISO)
        type: text
        default: "2026-03-09T08:00:00"
      - name: output_format
        label: Format
        type: select
        options:
          - html
          - json
          - yaml
        default: html
        
  - type: markdown
    content: |
      ### 📥 Export Options
      
      **JSON**: Machine-readable, can be shared with developer
      **YAML**: Human-readable structure
      **HTML**: Visual report with timeline
      
      After export, files are available at:
      `/local/ev_optimizer_debug_TIMESTAMP.format`
```

## Simple Button-Only Card

```yaml
type: entities
title: EV Debug Tools
entities:
  - entity: button.ev_optimizer_capture_debug_snapshot
    name: 📸 Snapshot Now
  - entity: button.ev_optimizer_export_debug_snapshots
    name: 💾 Export Snapshots
  - entity: button.ev_optimizer_dump_debug_state
    name: 🐛 Dump State to Logs
  - type: divider
  - type: button
    name: "🎬 Replay Last Night"
    icon: mdi:replay
    tap_action:
      action: call-service
      service: ev_optimizer.replay_scenario
      service_data:
        start_time: "{{ (now() - timedelta(hours=14)).strftime('%Y-%m-%dT18:00:00') }}"
        end_time: "{{ now().strftime('%Y-%m-%dT08:00:00') }}"
        output_format: html
```

## How to Use

### 1. Add the Card to Your Dashboard

1. Open your Home Assistant dashboard
2. Enter edit mode (top right button icon)
3. Click "+ Add Card"
4. Choose "Manual" card type
5. Paste one of the YAML configurations above
6. Save

### 2. Automatic Snapshot Capture

Snapshots are captured automatically:
- **Every hour** when car is NOT plugged in (light monitoring)
- **Every update (30 seconds)** when car IS plugged in (detailed monitoring)
- Creating full hourly snapshots with all changes tracked

Snapshots are kept for **7 days** automatically, then cleaned up.

### 3. Export Snapshots for Analysis

Click "Export Debug Snapshots" button or call the service:

```yaml
service: ev_optimizer.export_snapshots
data:
  start_time: "2026-03-08T00:00:00"  # Optional, defaults to 7 days ago
  end_time: "2026-03-09T23:59:59"    # Optional, defaults to now
  output_format: json  # json, yaml, or html
```

File will be created in `www/` directory and accessible at:
`http://your-ha-ip:8123/local/ev_optimizer_debug_TIMESTAMP.format`

### 4. Replay a Scenario

To understand why charging didn't happen:

```yaml
service: ev_optimizer.replay_scenario
data:
  start_time: "2026-03-08T18:00:00"  # Evening before
  end_time: "2026-03-09T08:00:00"    # Morning after
  output_format: html
```

This generates an HTML report showing:
- All snapshots in the time range
- SoC changes over time
- Charging decisions and why
- Available current at each hour
- Calendar events
- Price data

### 5. Download and Share

After export:
1. Open your browser to `http://your-ha-ip:8123/local/`
2. Find the `ev_optimizer_debug_*.json` or `*.html` file
3. Download it
4. Share JSON file with developer for analysis

## Understanding the Output

### JSON Export Structure

```json
{
  "export_info": {
    "created_at": "2026-03-09T10:00:00",
    "start_time": "2026-03-08T18:00:00",
    "end_time": "2026-03-09T08:00:00",
    "snapshot_count": 15
  },
  "snapshots": [
    {
      "timestamp": "2026-03-08T18:00:00",
      "car_soc": 57,
      "planned_target_soc": 80,
      "should_charge_now": false,
      "departure_time": "2026-03-09T07:00:00",
      "charging_summary": "Waiting for cheaper prices...",
      "changes_this_hour": [
        {
          "timestamp": "2026-03-08T18:00:23",
          "should_charge_now": false,
          "actions": []
        }
      ]
    }
  ],
  "prices": {
    "2026-03-08": {
      "today": [0.45, 0.47, ...],
      "tomorrow": [0.42, 0.44, ...]
    }
  }
}
```

### HTML Report

The HTML report shows:
- **Timeline**: Visual timeline of snapshots
- **Charging Status**: Green badge for charging, orange for paused
- **Metrics**: SoC, target, available current, for each hour
- **Summary**: Charging decision explanation
- **Changes**: Number of updates within each hour

## Troubleshooting

### No Snapshots Found

- Snapshots only kept for 7 days
- Check if car was plugged in during the time range
- Try manual snapshot capture first

### Export Failed

- Check Home Assistant logs for errors
- Ensure `www/` directory exists and is writable
- Check disk space

### Can't Access /local/ Files

- Ensure Home Assistant has www/ folder: `config/www/`
- Try accessing directly: `http://your-ha-ip:8123/local/filename.json`
- Check file permissions

## Advanced: Programmatic Access

You can access snapshots programmatically in automations
:

```yaml
automation:
  - alias: "Export Snapshots Daily"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: ev_optimizer.export_snapshots
        data:
          start_time: "{{ (now() - timedelta(days=1)).isoformat() }}"
          end_time: "{{ now().isoformat() }}"
          output_format: json
      - service: notify.mobile_app
        data:
          message: "EV Debug snapshots exported"
```

## Tips for Debugging

1. **After a failed charging night**:
   - Immediately export snapshots before they're auto-cleaned
   - Use HTML format for quick visual inspection
   - Use JSON format to share with others

2. **Comparing two nights**:
   - Export both time ranges separately
   - Compare the charging_summary fields
   - Check if calendar events differ
   - Verify price data was available

3. **Identifying Patterns**:
   - Export full week as JSON
   - Use text editor to search for "Waiting for"
   - Check when "should_charge_now" was actually true

## Next Steps

Once you have exported snapshots:
1. Review HTML report for obvious issues
2. Check if prices were available for planning
3. Verify calendar events were correct
4. Look for "Waiting for additional price data" messages
5. Share JSON with developer if issue persists
