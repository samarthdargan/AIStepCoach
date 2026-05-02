import asyncio
import json
from openai import OpenAI
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from dotenv import load_dotenv
from datetime import datetime
import os

load_dotenv()

client_ai = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("NVIDIA_API_KEY")
)

def generate_smart_nudge(
    steps: int,
    deficit: int,
    pace_needed: int,
    hours_left: int,
    week_data: dict | None = None,
    pace_data: dict | None = None,
) -> str:
    """Use an LLM to generate a personalised walking nudge that references
    the user's actual 7-day history and current pace vs typical."""

    week_data = week_data or {}
    pace_data = pace_data or {}

    # Best / worst days this week (for richer context).
    best_line = worst_line = ""
    days = week_data.get("days") or []
    if days:
        best = max(days, key=lambda d: d.get("steps", 0))
        worst = min(days, key=lambda d: d.get("steps", 0))
        try:
            best_dow = datetime.strptime(best["date"], "%Y-%m-%d").strftime("%A")
            worst_dow = datetime.strptime(worst["date"], "%Y-%m-%d").strftime("%A")
        except (KeyError, ValueError):
            best_dow = best.get("date", "")
            worst_dow = worst.get("date", "")
        best_line = f"- Best day this week: {best_dow} ({best.get('steps', 0):,} steps)"
        worst_line = f"- Slowest day this week: {worst_dow} ({worst.get('steps', 0):,} steps)"

    # Pace vs typical line.
    pace_line = ""
    expected = int(pace_data.get("expected_steps", 0))
    delta = int(pace_data.get("delta", 0))
    pct = float(pace_data.get("pct_of_typical", 0.0))
    if expected > 0:
        if pace_data.get("ahead_of_pace"):
            pace_line = (
                f"- Today's pace: AHEAD of usual by {delta:,} steps "
                f"({pct:.0f}% of typical for this hour)"
            )
        else:
            pace_line = (
                f"- Today's pace: BEHIND usual by {abs(delta):,} steps "
                f"({pct:.0f}% of typical for this hour)"
            )

    streak = int(week_data.get("streak", 0))
    avg = int(week_data.get("avg_steps", 0))

    prompt = f"""You are a friendly, sharp fitness coach giving a short walking nudge.

Today right now:
- Steps so far: {steps:,}
- Steps remaining to 10,000 goal: {deficit:,}
- Hours left in day: {hours_left}
- Required pace from now: {pace_needed:,} steps/hr
{pace_line}

Last 7 days context:
- Hit 10K goal on: {streak}/7 days
- Daily average: {avg:,} steps
{best_line}
{worst_line}

Write ONE short motivating message (max 25 words).
- Reference ONE concrete number from the context above (pace vs typical, streak, or best day).
- Don't be generic. Don't repeat all the numbers. Pick the most useful angle.
- If they're behind their typical pace, acknowledge it directly and give a clear next step.
- If they're ahead, celebrate it and push to extend the lead.
- Reply with just the message — no labels, no quotes, no preamble."""

    response = client_ai.chat.completions.create(
        model="meta/llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=80,
    )

    return response.choices[0].message.content.strip()


async def run_agent():
    server_params = StdioServerParameters(
        command="python", args=["mcp_server.py"]
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("✅ Connected to Step Nudge MCP Server\n")

            tools_result = await session.list_tools()
            tools = tools_result.tools
            print(f"📦 Tools loaded: {[t.name for t in tools]}\n")

            print("🤖 Prompt: Check my steps, save progress,")
            print("   generate smart nudge, and show dashboard.\n")

            # ── Step 1: Fetch today's steps ──────────────
            print("── Step 1: Fetching today's steps from Google Fit ──")
            result1 = await session.call_tool("fetch_steps_today", {})
            data = json.loads(result1.content[0].text)
            print(f"   ✅ Steps:       {data['steps_today']:,}")
            print(f"   ✅ Deficit:     {data['deficit']:,}")
            print(f"   ✅ Pace needed: {data['pace_needed_per_hour']:,} steps/hr")
            print(f"   ✅ Hours left:  {data['hours_left']} hrs\n")

            # ── Step 1b: Fetch last 7 days from Google Fit ─
            print("── Step 1b: Fetching last 7 days from Google Fit ──")
            result_week = await session.call_tool("fetch_steps_week", {})
            week_data = json.loads(result_week.content[0].text)
            for d in week_data["days"]:
                marker = "✅" if d["hit_target"] else "  "
                print(f"   {marker} {d['date']}: {d['steps']:,} steps")
            print(f"   📊 Streak: {week_data['streak']}/7 days  •  "
                  f"Avg {week_data['avg_steps']:,}/day  •  "
                  f"Total {week_data['total_steps']:,}\n")

            # ── Step 1c: Today's pace vs 7-day average ───
            print("── Step 1c: Comparing today's pace vs 7-day average ──")
            try:
                result_pace = await session.call_tool("fetch_pace_vs_average", {})
                pace_data = json.loads(result_pace.content[0].text)
                arrow = "🟢 AHEAD" if pace_data["ahead_of_pace"] else "🟠 BEHIND"
                sign = "+" if pace_data["delta"] >= 0 else "−"
                print(f"   {arrow}  {sign}{abs(pace_data['delta']):,} steps  "
                      f"({pace_data['pct_of_typical']:.0f}% of typical at "
                      f"{pace_data['current_hour']:02d}:00)")
                print(f"   Today: {pace_data['current_steps']:,}  •  "
                      f"Typical: {pace_data['expected_steps']:,}\n")
            except Exception as e:
                print(f"   ⚠️  Pace lookup failed: {e}\n")
                pace_data = {}

            # ── Step 2: Save log ─────────────────────────
            print("── Step 2: Saving to log ──")
            result2 = await session.call_tool("save_step_log", {
                "steps": data["steps_today"],
                "deficit": data["deficit"],
                "percentage": data["percentage"]
            })
            print(f"   ✅ {result2.content[0].text}\n")

            # ── Step 3: Generate smart nudge via LLM ─────
            print("── Step 3: Generating smart nudge via LLM ──")
            if data["deficit"] <= 0:
                streak = week_data.get("streak", 0)
                nudge = (
                    f"You crushed 10K today — that's {streak}/7 this week. "
                    "Keep the streak alive tomorrow!"
                )
            else:
                nudge = generate_smart_nudge(
                    steps=data["steps_today"],
                    deficit=data["deficit"],
                    pace_needed=data["pace_needed_per_hour"],
                    hours_left=data["hours_left"],
                    week_data=week_data,
                    pace_data=pace_data,
                )
            print(f"   🤖 {nudge}\n")

            # ── Step 4: Send Mac nudge ───────────────────
            print("── Step 4: Sending Mac notification ──")
            result3 = await session.call_tool("send_nudge", {
                "steps": data["steps_today"],
                "deficit": data["deficit"],
                "pace_needed": data["pace_needed_per_hour"],
                "hours_left": data["hours_left"]
            })
            print(f"   ✅ {result3.content[0].text}\n")

            # ── Step 5: Show dashboard ───────────────────
            print("── Step 5: Opening dashboard ──")
            result4 = await session.call_tool("show_dashboard", {
                "steps": data["steps_today"],
                "deficit": data["deficit"],
                "percentage": data["percentage"],
                "pace_needed": data["pace_needed_per_hour"],
                "hours_left": data["hours_left"],
                "nudge_message": nudge,
                "weekly_data_json": json.dumps(week_data["days"]),
                "pace_data_json": json.dumps(pace_data) if pace_data else "{}",
            })
            print(f"   ✅ {result4.content[0].text}\n")

            # ── Final summary ────────────────────────────
            print("=" * 40)
            print("✅ AGENT COMPLETE")
            print("=" * 40)
            print(f"🦶 Steps:     {data['steps_today']:,} / 10,000")
            print(f"📊 Progress:  {data['percentage']}%")
            print(f"🔥 Streak:    {week_data['streak']}/7 days  (avg {week_data['avg_steps']:,}/day)")
            if pace_data.get("expected_steps"):
                arrow = "🟢" if pace_data["ahead_of_pace"] else "🟠"
                sign = "+" if pace_data["delta"] >= 0 else "−"
                print(f"📈 Pace:      {arrow} {sign}{abs(pace_data['delta']):,} vs typical "
                      f"({pace_data['pct_of_typical']:.0f}%)")
            print(f"💬 Nudge:     {nudge}")
            print(f"🌐 Dashboard: sandbox/dashboard.html")


async def main():
    while True:
        print(f"\n🕐 Running at {datetime.now().strftime('%H:%M')}\n")
        await run_agent()
        print("\n💤 Next nudge in 5 minutes...\n")
        await asyncio.sleep(300)  # 300 seconds = 5 mins


asyncio.run(main())
