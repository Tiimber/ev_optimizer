"""Snapshot manager for debugging and replay functionality.

Captures system state over time to enable:
- Historical debugging of charging decisions
- Replay/simulation of past scenarios
- Export of debug data for analysis
"""
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)


class SnapshotManager:
    """Manage historical snapshots for debugging."""
    
    SNAPSHOT_VERSION = 1
    MAX_AGE_DAYS = 7
    
    def __init__(self, hass: HomeAssistant, entry_id: str):
        """Initialize snapshot manager."""
        self.hass = hass
        self.entry_id = entry_id
        self.store = Store(hass, self.SNAPSHOT_VERSION, f"{DOMAIN}.{entry_id}.snapshots")
        self.price_store = Store(hass, self.SNAPSHOT_VERSION, f"{DOMAIN}.{entry_id}.prices")
        
        # Runtime data
        self.snapshots: list[dict] = []  # Hourly snapshots
        self.prices_by_date: dict[str, dict] = {}  # {date_str: {today: [], tomorrow: []}}
        self.current_hour_changes: list[dict] = []  # Changes within current hour
        self.last_snapshot_hour: datetime | None = None
        
    async def async_load(self):
        """Load existing snapshots and prices from storage."""
        try:
            # Load snapshots
            snapshot_data = await self.store.async_load()
            if snapshot_data:
                self.snapshots = snapshot_data.get("snapshots", [])
                _LOGGER.info(f"Loaded {len(self.snapshots)} snapshots from storage")
                
            # Load prices
            price_data = await self.price_store.async_load()
            if price_data:
                self.prices_by_date = price_data.get("prices", {})
                _LOGGER.info(f"Loaded prices for {len(self.prices_by_date)} dates")
                
            # Cleanup old data
            await self.cleanup_old_snapshots()
            
        except Exception as e:
            _LOGGER.warning(f"Failed to load snapshots: {e}")
            self.snapshots = []
            self.prices_by_date = {}
    
    async def capture_snapshot(
        self, 
        coordinator_data: dict,
        plan: dict,
        actions_taken: list[str] | None = None
    ):
        """Capture current state.
        
        Called every coordinator update. Collects changes and creates
        full hourly snapshot when hour boundary crossed.
        
        Args:
            coordinator_data: Full coordinator data dict
            plan: Current charging plan
            actions_taken: List of action messages from this update
        """
        now = dt_util.now()
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        
        # Store prices separately (only when they change)
        await self._store_prices_if_new(coordinator_data.get("price_data", {}), now)
        
        # Create change record for this update
        change_record = {
            "timestamp": now.isoformat(),
            "car_soc": coordinator_data.get("car_soc"),
            "car_plugged": coordinator_data.get("car_plugged"),
            "should_charge_now": plan.get("should_charge_now"),
            "planned_target_soc": plan.get("planned_target_soc"),
            "max_available_current": coordinator_data.get("max_available_current"),
            "actions": actions_taken or [],
        }
        
        self.current_hour_changes.append(change_record)
        
        # Check if we crossed an hour boundary
        if self.last_snapshot_hour is None or current_hour > self.last_snapshot_hour:
            await self._create_hourly_snapshot(coordinator_data, plan, current_hour)
            self.last_snapshot_hour = current_hour
            self.current_hour_changes = []
    
    async def _create_hourly_snapshot(
        self,
        coordinator_data: dict,
        plan: dict,
        snapshot_time: datetime
    ):
        """Create a full hourly snapshot."""
        now_str = snapshot_time.date().isoformat()
        
        snapshot = {
            "timestamp": snapshot_time.isoformat(),
            "version": self.SNAPSHOT_VERSION,
            
            # Car state
            "car_soc": coordinator_data.get("car_soc"),
            "car_plugged": coordinator_data.get("car_plugged"),
            "virtual_soc": coordinator_data.get("virtual_soc"),
            
            # Charging state
            "smart_charging_enabled": coordinator_data.get("smart_charging_enabled"),
            "should_charge_now": plan.get("should_charge_now"),
            "planned_target_soc": plan.get("planned_target_soc"),
            "departure_time": plan.get("departure_time"),
            "scheduled_start": plan.get("scheduled_start"),
            "session_end_time": plan.get("session_end_time"),
            
            # Targets and limits
            "target_soc": coordinator_data.get("target_soc"),
            "target_override": coordinator_data.get("target_override"),
            "min_soc": coordinator_data.get("min_soc"),
            "departure_time_setting": coordinator_data.get("departure_time"),
            
            # Load balancing
            "max_available_current": coordinator_data.get("max_available_current"),
            "house_consumption": coordinator_data.get("house_consumption"),
            
            # Manual overrides
            "manual_override_active": coordinator_data.get("manual_override_active"),
            
            # Calendar (anonymized)
            "calendar_events": self._anonymize_calendar(
                coordinator_data.get("calendar_events", [])
            ),
            
            # Price reference (date only, actual prices stored separately)
            "price_date": now_str,
            
            # Plan summary
            "charging_summary": plan.get("charging_summary", ""),
            
            # Learning state
            "learned_efficiency": coordinator_data.get("learned_efficiency"),
            "learning_confidence": coordinator_data.get("learning_confidence"),
            
            # Session data
            "session_number": coordinator_data.get("session_number"),
            "overload_prevention_minutes": coordinator_data.get("overload_prevention_minutes"),
            
            # Changes during this hour
            "changes_this_hour": self.current_hour_changes.copy(),
        }
        
        self.snapshots.append(snapshot)
        _LOGGER.debug(f"Created hourly snapshot at {snapshot_time}")
        
        # Save to storage (async, with delay to batch writes)
        await self._save_snapshots()
    
    def _anonymize_calendar(self, events: list[dict]) -> list[dict]:
        """Anonymize calendar events - keep timing and SoC target only."""
        anonymized = []
        for event in events:
            # Hash the summary for consistent anonymization
            summary = event.get("summary", "")
            summary_hash = hashlib.md5(summary.encode()).hexdigest()[:8]
            
            # Extract percentage if present
            description = event.get("description", "")
            combined_text = f"{summary} {description}"
            import re
            match = re.search(r"(\d+)\s*%", combined_text)
            target_soc = int(match.group(1)) if match else None
            
            anonymized.append({
                "start": event.get("start"),
                "end": event.get("end"),
                "label_hash": summary_hash,
                "target_soc_pct": target_soc,
            })
        
        return anonymized
    
    async def _store_prices_if_new(self, price_data: dict, now: datetime):
        """Store prices separately by date (only if not already stored)."""
        date_str = now.date().isoformat()
        
        # Check if we already have prices for this date
        if date_str in self.prices_by_date:
            return
        
        # Store today and tomorrow prices
        today_prices = price_data.get("today", [])
        tomorrow_prices = price_data.get("tomorrow", [])
        tomorrow_valid = price_data.get("tomorrow_valid", False)
        
        if today_prices:  # Only store if we have data
            self.prices_by_date[date_str] = {
                "today": today_prices,
                "tomorrow": tomorrow_prices,
                "tomorrow_valid": tomorrow_valid,
                "stored_at": now.isoformat(),
            }
            
            _LOGGER.debug(f"Stored prices for {date_str}: {len(today_prices)} today, {len(tomorrow_prices)} tomorrow")
            await self._save_prices()
    
    async def _save_snapshots(self):
        """Save snapshots to storage."""
        data = {
            "version": self.SNAPSHOT_VERSION,
            "snapshots": self.snapshots,
            "last_updated": dt_util.now().isoformat(),
        }
        self.store.async_delay_save(lambda: data, 2.0)
    
    async def _save_prices(self):
        """Save prices to storage."""
        data = {
            "version": self.SNAPSHOT_VERSION,
            "prices": self.prices_by_date,
            "last_updated": dt_util.now().isoformat(),
        }
        self.price_store.async_delay_save(lambda: data, 2.0)
    
    async def cleanup_old_snapshots(self):
        """Remove snapshots and prices older than MAX_AGE_DAYS."""
        cutoff = dt_util.now() - timedelta(days=self.MAX_AGE_DAYS)
        cutoff_str = cutoff.date().isoformat()
        
        # Clean snapshots
        original_count = len(self.snapshots)
        self.snapshots = [
            s for s in self.snapshots
            if datetime.fromisoformat(s["timestamp"]) > cutoff
        ]
        
        if len(self.snapshots) < original_count:
            removed = original_count - len(self.snapshots)
            _LOGGER.info(f"Removed {removed} old snapshots (>{self.MAX_AGE_DAYS} days)")
            await self._save_snapshots()
        
        # Clean prices
        original_price_count = len(self.prices_by_date)
        self.prices_by_date = {
            date: prices
            for date, prices in self.prices_by_date.items()
            if date >= cutoff_str
        }
        
        if len(self.prices_by_date) < original_price_count:
            removed = original_price_count - len(self.prices_by_date)
            _LOGGER.info(f"Removed prices for {removed} old dates")
            await self._save_prices()
    
    async def export_snapshots(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        output_format: str = "json"
    ) -> str:
        """Export snapshots to www/ directory.
        
        Args:
            start_time: Start of time range (default: 7 days ago)
            end_time: End of time range (default: now)
            output_format: "json", "yaml", or "html"
            
        Returns:
            Path to exported file
        """
        if start_time is None:
            start_time = dt_util.now() - timedelta(days=7)
        if end_time is None:
            end_time = dt_util.now()
        
        # Filter snapshots
        filtered_snapshots = [
            s for s in self.snapshots
            if start_time <= datetime.fromisoformat(s["timestamp"]) <= end_time
        ]
        
        # Build export data
        export_data = {
            "export_info": {
                "created_at": dt_util.now().isoformat(),
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "snapshot_count": len(filtered_snapshots),
                "version": self.SNAPSHOT_VERSION,
            },
            "snapshots": filtered_snapshots,
            "prices": self.prices_by_date,
        }
        
        # Create www directory if needed
        www_path = Path(self.hass.config.path("www"))
        www_path.mkdir(exist_ok=True)
        
        timestamp = dt_util.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ev_optimizer_debug_{timestamp}.{output_format}"
        filepath = www_path / filename
        
        # Write file
        if output_format == "json":
            with open(filepath, "w") as f:
                json.dump(export_data, f, indent=2)
        elif output_format == "yaml":
            import yaml
            with open(filepath, "w") as f:
                yaml.dump(export_data, f, default_flow_style=False)
        elif output_format == "html":
            html = self._generate_html_report(export_data)
            with open(filepath, "w") as f:
                f.write(html)
        else:
            raise ValueError(f"Unsupported format: {output_format}")
        
        _LOGGER.info(f"Exported {len(filtered_snapshots)} snapshots to {filename}")
        return f"/local/{filename}"
    
    def _generate_html_report(self, export_data: dict) -> str:
        """Generate HTML report from export data."""
        snapshots = export_data["snapshots"]
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>EV Optimizer Debug Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .header {{ background: #1976d2; color: white; padding: 20px; border-radius: 8px; }}
        .snapshot {{ background: white; margin: 10px 0; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .timestamp {{ font-weight: bold; color: #1976d2; }}
        .charging {{ background: #4caf50; color: white; padding: 2px 8px; border-radius: 4px; }}
        .paused {{ background: #ff9800; color: white; padding: 2px 8px; border-radius: 4px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 10px; }}
        .metric {{ background: #f0f0f0; padding: 8px; border-radius: 4px; }}
        .label {{ font-size: 0.85em; color: #666; }}
        .value {{ font-size: 1.1em; font-weight: bold; }}
        .changes {{ background: #fff3cd; padding: 10px; margin-top: 10px; border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>EV Optimizer Debug Report</h1>
        <p>Generated: {export_data['export_info']['created_at']}</p>
        <p>Time Range: {export_data['export_info']['start_time']} to {export_data['export_info']['end_time']}</p>
        <p>Snapshots: {export_data['export_info']['snapshot_count']}</p>
    </div>
"""
        
        for snapshot in snapshots:
            charging_badge = (
                '<span class="charging">CHARGING</span>' 
                if snapshot.get("should_charge_now") 
                else '<span class="paused">PAUSED</span>'
            )
            
            html += f"""
    <div class="snapshot">
        <div class="timestamp">{snapshot['timestamp']} {charging_badge}</div>
        <div class="grid">
            <div class="metric">
                <div class="label">Car SoC</div>
                <div class="value">{snapshot.get('car_soc', 'N/A')}%</div>
            </div>
            <div class="metric">
                <div class="label">Target SoC</div>
                <div class="value">{snapshot.get('planned_target_soc', 'N/A')}%</div>
            </div>
            <div class="metric">
                <div class="label">Available Current</div>
                <div class="value">{snapshot.get('max_available_current', 'N/A')}A</div>
            </div>
            <div class="metric">
                <div class="label">Departure</div>
                <div class="value">{snapshot.get('departure_time', 'N/A')}</div>
            </div>
        </div>
        <p><strong>Summary:</strong> {snapshot.get('charging_summary', 'N/A')[:200]}</p>
"""
            
            if snapshot.get('changes_this_hour'):
                html += f"""
        <div class="changes">
            <strong>Changes this hour:</strong> {len(snapshot['changes_this_hour'])} updates
        </div>
"""
            
            html += "    </div>\n"
        
        html += """
</body>
</html>
"""
        return html
    
    def get_snapshot_at_time(self, target_time: datetime) -> dict | None:
        """Get the snapshot closest to the target time."""
        if not self.snapshots:
            return None
        
        # Find closest snapshot
        closest = min(
            self.snapshots,
            key=lambda s: abs(datetime.fromisoformat(s["timestamp"]) - target_time)
        )
        
        return closest
    
    def get_snapshots_in_range(
        self,
        start_time: datetime,
        end_time: datetime
    ) -> list[dict]:
        """Get all snapshots within time range."""
        return [
            s for s in self.snapshots
            if start_time <= datetime.fromisoformat(s["timestamp"]) <= end_time
        ]
