from mcp.server.fastmcp import FastMCP
from datetime import datetime, timedelta
import json, pickle, time, subprocess
from googleapiclient.discovery import build

mcp = FastMCP("Step Nudge Agent")

# ── Load Google credentials ──────────────────────
def get_google_creds():
    with open('token.pkl', 'rb') as f:
        return pickle.load(f)

# ── TOOL 1: Fetch steps from Google Fit ──────────
@mcp.tool()
def fetch_steps_today() -> dict:
    """Fetch today's real step count from Google Fit API"""

    creds = get_google_creds()
    service = build('fitness', 'v1', credentials=creds)

    now_ms = int(time.time() * 1000)
    midnight_ms = now_ms - (now_ms % 86400000)

    body = {
        "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": midnight_ms,
        "endTimeMillis": now_ms
    }

    result = service.users().dataset().aggregate(
        userId="me", body=body
    ).execute()

    try:
        steps = result['bucket'][0]['dataset'][0]['point'][0]['value'][0]['intVal']
    except (IndexError, KeyError):
        steps = 0

    target = 10000
    deficit = target - steps
    hour = datetime.now().hour
    hours_left = max(23 - hour, 1)
    pace_needed = deficit // hours_left

    return {
        "steps_today": steps,
        "target": target,
        "deficit": max(deficit, 0),
        "percentage": round((steps / target) * 100, 1),
        "pace_needed_per_hour": max(pace_needed, 0),
        "hours_left": hours_left,
        "current_time": datetime.now().strftime("%H:%M")
    }

# ── TOOL 1b: Fetch last 7 days from Google Fit ───
@mcp.tool()
def fetch_steps_week() -> dict:
    """Fetch real per-day step counts for the last 7 days from Google Fit.

    Returns a dict with key 'days', a list of 7 entries (oldest -> newest):
        [{"date": "YYYY-MM-DD", "steps": int, "hit_target": bool}, ...]
    Today is the last entry (may be partial)."""

    creds = get_google_creds()
    service = build('fitness', 'v1', credentials=creds)

    now_ms = int(time.time() * 1000)
    midnight_today_ms = now_ms - (now_ms % 86400000)
    # 7 buckets: 6 prior full days + today (partial)
    start_ms = midnight_today_ms - (6 * 86400000)
    end_ms = now_ms

    body = {
        "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": start_ms,
        "endTimeMillis": end_ms,
    }

    result = service.users().dataset().aggregate(
        userId="me", body=body
    ).execute()

    days = []
    target = 10000
    for bucket in result.get('bucket', []):
        bucket_start = int(bucket['startTimeMillis'])
        date_str = datetime.fromtimestamp(bucket_start / 1000).strftime("%Y-%m-%d")
        steps = 0
        try:
            steps = int(bucket['dataset'][0]['point'][0]['value'][0]['intVal'])
        except (IndexError, KeyError, ValueError):
            steps = 0
        days.append({
            "date": date_str,
            "steps": steps,
            "hit_target": steps >= target,
        })

    # Pad to exactly 7 entries if Fit returned fewer (e.g. no data for some days)
    if len(days) < 7:
        existing = {d["date"]: d for d in days}
        days = []
        for i in range(6, -1, -1):
            d_date = (datetime.fromtimestamp(midnight_today_ms / 1000)
                      - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(existing.get(d_date, {
                "date": d_date, "steps": 0, "hit_target": False,
            }))

    streak = sum(1 for d in days if d["hit_target"])
    total = sum(d["steps"] for d in days)
    avg = total // 7 if days else 0

    return {
        "days": days,
        "streak": streak,
        "total_steps": total,
        "avg_steps": avg,
        "target": target,
    }

# ── TOOL 1c: Compare today's pace vs 7-day average ─
@mcp.tool()
def fetch_pace_vs_average() -> dict:
    """Compare today's cumulative step pace at the current hour to the
    7-day historical average at the same hour. Pulls hourly buckets from
    Google Fit and computes a 'typical' cumulative curve.

    Returns:
        {
          "current_hour": int,           # 0-23
          "current_steps": int,          # today by now (hourly aggregate)
          "expected_steps": int,         # 7-day avg cumulative by current hour
          "delta": int,                  # current - expected (negative = behind)
          "ahead_of_pace": bool,
          "pct_of_typical": float,       # current / expected * 100
          "today_cumulative": list[int], # 24 ints (cumulative steps by hour, today)
          "typical_cumulative": list[int], # 24 ints (avg cumulative across 7 days)
        }
    """
    creds = get_google_creds()
    service = build('fitness', 'v1', credentials=creds)

    now = datetime.now()
    now_ms = int(time.time() * 1000)
    midnight_today_ms = now_ms - (now_ms % 86400000)
    # 7 prior full days + today (hourly buckets)
    start_ms = midnight_today_ms - (7 * 86400000)
    end_ms = now_ms

    body = {
        "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
        "bucketByTime": {"durationMillis": 3600000},  # 1 hour
        "startTimeMillis": start_ms,
        "endTimeMillis": end_ms,
    }

    result = service.users().dataset().aggregate(
        userId="me", body=body
    ).execute()

    # Group hourly steps by date.
    by_date: dict[str, list[int]] = {}
    for bucket in result.get("bucket", []):
        ts = int(bucket["startTimeMillis"]) / 1000
        dt = datetime.fromtimestamp(ts)
        date_key = dt.strftime("%Y-%m-%d")
        hour = dt.hour
        try:
            steps = int(bucket["dataset"][0]["point"][0]["value"][0]["intVal"])
        except (IndexError, KeyError, ValueError):
            steps = 0
        if date_key not in by_date:
            by_date[date_key] = [0] * 24
        by_date[date_key][hour] = steps

    today_key = now.strftime("%Y-%m-%d")
    today_hourly = by_date.pop(today_key, [0] * 24)

    historical_dates = sorted(by_date.keys())[-7:]
    if historical_dates:
        historical_hourly = [
            sum(by_date[d][h] for d in historical_dates) / len(historical_dates)
            for h in range(24)
        ]
    else:
        historical_hourly = [0.0] * 24

    today_cum: list[int] = []
    running = 0
    for h in today_hourly:
        running += h
        today_cum.append(running)

    typical_cum: list[int] = []
    running_f = 0.0
    for h in historical_hourly:
        running_f += h
        typical_cum.append(int(round(running_f)))

    current_hour = min(now.hour, 23)
    current_steps = today_cum[current_hour]
    expected_steps = typical_cum[current_hour]
    delta = current_steps - expected_steps
    pct_of_typical = (
        round((current_steps / expected_steps) * 100, 1)
        if expected_steps > 0 else 0.0
    )

    return {
        "current_hour": current_hour,
        "current_steps": current_steps,
        "expected_steps": expected_steps,
        "delta": delta,
        "ahead_of_pace": delta >= 0,
        "pct_of_typical": pct_of_typical,
        "today_cumulative": today_cum,
        "typical_cumulative": typical_cum,
    }

# ── TOOL 2: Save step log ─────────────────────────
@mcp.tool()
def save_step_log(steps: int, deficit: int, percentage: float) -> str:
    """Save hourly step snapshot to local log file"""

    try:
        with open("sandbox/step_log.json", "r") as f:
            log = json.load(f)
    except:
        log = {}

    today = datetime.now().strftime("%Y-%m-%d")
    hour  = datetime.now().strftime("%H:00")

    if today not in log:
        log[today] = {}

    log[today][hour] = {
        "steps": steps,
        "deficit": deficit,
        "percentage": percentage,
        "on_track": steps >= (10000 * (datetime.now().hour / 24))
    }

    with open("sandbox/step_log.json", "w") as f:
        json.dump(log, f, indent=2)

    return f"Logged {steps} steps at {hour} — {percentage}% of target"

# ── TOOL 3: Send Mac nudge notification ───────────
@mcp.tool()
def send_nudge(steps: int, deficit: int, pace_needed: int, hours_left: int) -> str:
    """Send Mac notification with walking plan"""

    if deficit <= 0:
        msg = "You hit your 10,000 step goal today! Great job!"
        title = "Goal Complete!"
    else:
        msg = (f"{deficit:,} steps left — "
               f"walk {pace_needed:,} steps/hr "
               f"for next {hours_left} hrs to hit 10K!")
        title = "Step Nudge"

    subprocess.run([
        'osascript', '-e',
        f'display notification "{msg}" with title "🚶 {title}"'
    ])

    return f"Nudge sent: {msg}"

# ── TOOL 4: Show Prefab dashboard ─────────────────
@mcp.tool()
def show_dashboard(
    steps: int,
    deficit: int,
    percentage: float,
    pace_needed: int,
    hours_left: int,
    nudge_message: str,
    weekly_data_json: str = "[]",
    pace_data_json: str = "{}",
) -> str:
    """Show a visual dashboard with today's progress + last-7-days history +
    today's pace vs the 7-day-average pace at the same hour.

    weekly_data_json: JSON list of 7 day entries from fetch_steps_week["days"].
    pace_data_json:   JSON dict from fetch_pace_vs_average (full return value).
                      If empty, the pace card is hidden."""

    # Prefer real Google Fit weekly data passed in; fall back to local log.
    weekly: list[dict] = []
    try:
        parsed = json.loads(weekly_data_json) if weekly_data_json else []
        if isinstance(parsed, list) and parsed:
            weekly = [
                {
                    "date": str(d.get("date", "")),
                    "steps": int(d.get("steps", 0)),
                    "hit_target": bool(d.get("hit_target", int(d.get("steps", 0)) >= 10000)),
                }
                for d in parsed
            ]
    except (ValueError, TypeError):
        weekly = []

    if not weekly:
        try:
            with open("sandbox/step_log.json", "r") as f:
                log = json.load(f)
        except:
            log = {}
        for date, hours in sorted(log.items())[-7:]:
            last_entry = list(hours.values())[-1]
            weekly.append({
                "date": date,
                "steps": last_entry["steps"],
                "hit_target": last_entry["steps"] >= 10000,
            })

    streak = sum(1 for d in weekly if d["hit_target"])
    week_total = sum(d["steps"] for d in weekly)
    week_avg = week_total // len(weekly) if weekly else 0

    # Build HTML dashboard
    today = datetime.now().strftime("%d %b %Y")
    time_now = datetime.now().strftime("%H:%M")

    # Weekly bars HTML
    today_date = datetime.now().strftime("%Y-%m-%d")
    weekly_bars = ""
    for d in weekly:
        bar_pct = min((d["steps"] / 10000) * 100, 100)
        color = "#22c55e" if d["hit_target"] else "#f97316"
        is_today = d["date"] == today_date
        try:
            dow = datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a")
        except ValueError:
            dow = d["date"][5:]
        label = "Today" if is_today else dow
        ring = "outline:2px solid #60a5fa; outline-offset:2px;" if is_today else ""
        weekly_bars += f"""
        <div style="display:flex;flex-direction:column;align-items:center;gap:4px">
            <div style="font-size:11px;color:#94a3b8">{d['steps']:,}</div>
            <div style="width:32px;background:#1e293b;border-radius:4px;height:80px;
                        display:flex;align-items:flex-end;{ring}">
                <div style="width:100%;height:{bar_pct}%;background:{color};
                            border-radius:4px;transition:height 0.3s"></div>
            </div>
            <div style="font-size:11px;color:{'#60a5fa' if is_today else '#94a3b8'};
                        font-weight:{'700' if is_today else '400'}">{label}</div>
        </div>"""

    # ── Pace-vs-Typical card ──────────────────────────
    pace_card_html = ""
    try:
        pace = json.loads(pace_data_json) if pace_data_json else {}
    except (ValueError, TypeError):
        pace = {}

    if pace.get("today_cumulative") and pace.get("typical_cumulative"):
        today_cum = [int(x) for x in pace["today_cumulative"]]
        typical_cum = [int(x) for x in pace["typical_cumulative"]]
        cur_hr = int(pace.get("current_hour", datetime.now().hour))
        cur_steps = int(pace.get("current_steps", 0))
        exp_steps = int(pace.get("expected_steps", 0))
        delta = int(pace.get("delta", 0))
        pct = float(pace.get("pct_of_typical", 0.0))
        ahead = bool(pace.get("ahead_of_pace", delta >= 0))

        w, h = 600, 160
        L, R, T, B = 36, w - 16, 16, h - 24
        plot_w, plot_h = R - L, B - T
        max_val = max(max(today_cum), max(typical_cum), 10000)

        def _x(hr: int) -> float:
            return L + (hr / 23) * plot_w

        def _y(v: float) -> float:
            return B - (min(v, max_val) / max_val) * plot_h

        today_pts = " ".join(
            f"{_x(i):.1f},{_y(v):.1f}"
            for i, v in enumerate(today_cum[:cur_hr + 1])
        )
        typical_pts = " ".join(
            f"{_x(i):.1f},{_y(v):.1f}"
            for i, v in enumerate(typical_cum)
        )
        goal_y = _y(10000)
        x_labels = "".join(
            f'<text x="{_x(hr):.1f}" y="{h - 6}" text-anchor="middle" '
            f'fill="#64748b" font-size="10">{hr:02d}h</text>'
            for hr in (0, 6, 12, 18, 23)
        )
        delta_color = "#22c55e" if ahead else "#f87171"
        delta_sign = "+" if ahead else "−"
        delta_label = f"{delta_sign}{abs(delta):,} vs typical"
        sub_label = f"{pct:.0f}% of typical pace by {cur_hr:02d}:00 (typical {exp_steps:,})"

        pace_card_html = f"""
        <!-- Pace vs Typical -->
        <div class="card full-width">
            <h2>📈 Pace vs Typical (last 7 days)</h2>
            <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:8px">
                <div style="font-size:28px;font-weight:800;color:{delta_color}">{delta_label}</div>
                <div style="font-size:13px;color:#94a3b8">{sub_label}</div>
            </div>
            <svg viewBox="0 0 {w} {h}" preserveAspectRatio="none"
                 style="width:100%;height:160px">
                <line x1="{L}" y1="{goal_y:.1f}" x2="{R}" y2="{goal_y:.1f}"
                      stroke="#22c55e" stroke-dasharray="4,4"
                      stroke-width="1" opacity="0.55"/>
                <text x="{R - 4}" y="{goal_y - 4:.1f}" text-anchor="end"
                      fill="#22c55e" font-size="10">10K goal</text>
                <polyline points="{typical_pts}" fill="none" stroke="#94a3b8"
                          stroke-width="2" stroke-dasharray="3,3" opacity="0.7"/>
                <polyline points="{today_pts}" fill="none" stroke="#f97316"
                          stroke-width="2.5"/>
                <line x1="{_x(cur_hr):.1f}" y1="{T}" x2="{_x(cur_hr):.1f}" y2="{B}"
                      stroke="#60a5fa" stroke-width="1"
                      stroke-dasharray="2,2" opacity="0.6"/>
                <circle cx="{_x(cur_hr):.1f}" cy="{_y(cur_steps):.1f}" r="4"
                        fill="#f97316"/>
                {x_labels}
            </svg>
            <div style="display:flex;gap:16px;justify-content:center;
                        font-size:12px;color:#94a3b8;margin-top:6px">
                <span><span style="display:inline-block;width:14px;height:2px;
                      background:#f97316;vertical-align:middle"></span>
                      &nbsp;Today ({cur_steps:,})</span>
                <span><span style="display:inline-block;width:14px;height:2px;
                      background:#94a3b8;vertical-align:middle;
                      border-top:2px dashed #94a3b8"></span>
                      &nbsp;Typical 7-day avg</span>
            </div>
        </div>"""

    # Progress ring calculation
    circumference = 2 * 3.14159 * 54
    progress_offset = circumference - (percentage / 100) * circumference
    ring_color = "#22c55e" if deficit <= 0 else "#f97316"
    status_emoji = "🎉" if deficit <= 0 else "⚠️"
    status_text = "Goal Complete!" if deficit <= 0 else f"{deficit:,} steps remaining"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Step Nudge Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #0f172a;
            color: #f1f5f9;
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            padding: 24px;
        }}
        .header {{
            text-align: center;
            margin-bottom: 24px;
        }}
        .header h1 {{
            font-size: 28px;
            font-weight: 700;
            color: #f97316;
        }}
        .header p {{
            color: #94a3b8;
            font-size: 14px;
            margin-top: 4px;
        }}
        .grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            max-width: 700px;
            margin: 0 auto;
        }}
        .card {{
            background: #1e293b;
            border-radius: 16px;
            padding: 20px;
            border: 1px solid #334155;
        }}
        .card h2 {{
            font-size: 13px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 16px;
        }}
        .ring-container {{
            display: flex;
            justify-content: center;
            align-items: center;
            flex-direction: column;
            gap: 12px;
        }}
        .steps-big {{
            font-size: 36px;
            font-weight: 800;
            color: #f97316;
        }}
        .steps-sub {{
            font-size: 14px;
            color: #94a3b8;
        }}
        .stat-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #334155;
        }}
        .stat-row:last-child {{ border-bottom: none; }}
        .stat-label {{ color: #94a3b8; font-size: 14px; }}
        .stat-value {{ font-weight: 700; font-size: 16px; }}
        .nudge-box {{
            background: #1e3a5f;
            border: 1px solid #3b82f6;
            border-radius: 12px;
            padding: 16px;
            font-size: 15px;
            line-height: 1.5;
            color: #bfdbfe;
        }}
        .weekly-bars {{
            display: flex;
            justify-content: space-around;
            align-items: flex-end;
            height: 120px;
            padding-top: 20px;
        }}
        .streak-badge {{
            background: #7c3aed;
            color: white;
            border-radius: 999px;
            padding: 6px 16px;
            font-size: 14px;
            font-weight: 700;
            display: inline-block;
            margin-top: 12px;
        }}
        .full-width {{ grid-column: span 2; }}
        .status-banner {{
            text-align: center;
            padding: 12px;
            background: {'#14532d' if deficit <= 0 else '#431407'};
            border-radius: 12px;
            border: 1px solid {'#22c55e' if deficit <= 0 else '#f97316'};
            margin-bottom: 8px;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🚶 Step Nudge</h1>
        <p>{today} • Updated at {time_now}</p>
    </div>

    <div class="grid">

        <!-- Progress Ring -->
        <div class="card">
            <h2>Today's Progress</h2>
            <div class="ring-container">
                <svg width="120" height="120" viewBox="0 0 120 120">
                    <circle cx="60" cy="60" r="54"
                        fill="none" stroke="#334155" stroke-width="12"/>
                    <circle cx="60" cy="60" r="54"
                        fill="none" stroke="{ring_color}" stroke-width="12"
                        stroke-dasharray="{circumference}"
                        stroke-dashoffset="{progress_offset}"
                        stroke-linecap="round"
                        transform="rotate(-90 60 60)"/>
                    <text x="60" y="55" text-anchor="middle"
                        fill="{ring_color}" font-size="22" font-weight="800">{percentage}%</text>
                    <text x="60" y="72" text-anchor="middle"
                        fill="#94a3b8" font-size="11">complete</text>
                </svg>
                <div class="steps-big">{steps:,}</div>
                <div class="steps-sub">of 10,000 steps</div>
                <div class="status-banner">{status_emoji} {status_text}</div>
            </div>
        </div>

        <!-- Stats -->
        <div class="card">
            <h2>Stats</h2>
            <div class="stat-row">
                <span class="stat-label">⏰ Hours left</span>
                <span class="stat-value" style="color:#60a5fa">{hours_left} hrs</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">🎯 Pace needed</span>
                <span class="stat-value" style="color:#f97316">{pace_needed:,}/hr</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">📉 Deficit</span>
                <span class="stat-value" style="color:#f87171">{deficit:,}</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">🔥 Week streak</span>
                <span class="stat-value" style="color:#a78bfa">{streak}/7 days</span>
            </div>
        </div>

        {pace_card_html}

        <!-- Smart Nudge -->
        <div class="card full-width">
            <h2>🤖 AI Nudge</h2>
            <div class="nudge-box">"{nudge_message}"</div>
        </div>

        <!-- Weekly Chart -->
        <div class="card full-width">
            <h2>📅 Last 7 Days (Google Fit)</h2>
            <div class="weekly-bars">
                {weekly_bars}
            </div>
            <div style="text-align:center;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
                <span class="streak-badge">🔥 {streak}/7 days hit goal</span>
                <span class="streak-badge" style="background:#0ea5e9">📊 Avg {week_avg:,}/day</span>
                <span class="streak-badge" style="background:#16a34a">∑ {week_total:,} this week</span>
            </div>
        </div>

    </div>
</body>
</html>"""

    # Save HTML file
    with open("sandbox/dashboard.html", "w") as f:
        f.write(html)

    # Open in browser
    subprocess.run(["open", "sandbox/dashboard.html"])

    return "✅ Dashboard opened in browser!"

if __name__ == "__main__":
    mcp.run()
