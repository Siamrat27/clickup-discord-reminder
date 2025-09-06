#!/usr/bin/env python3
# clickup_daily_to_discord.py
# Purpose: Fetch ClickUp tasks and post a summary to Discord.
# - Exams (#exam) -> next EXAM_DAYS_AHEAD days (default 14)
# - Others       -> next DAYS_AHEAD days (default 7)
# - Adds Groq AI summary at top if GROQ_API_KEY is set
# - Splits long messages to avoid Discord 2000-char limit

import os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

API_BASE = "https://api.clickup.com/api/v2"

# ---------- .env loader ----------
def load_dotenv(path: str = ".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")

# ---------- ClickUp helpers ----------
def get_my_user_id(headers):
    resp = requests.get(f"{API_BASE}/user", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["user"]["id"]

def get_teams(headers):
    resp = requests.get(f"{API_BASE}/team", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("teams", [])

def fetch_due_tasks(headers, team_id, due_start_ms, due_end_ms, assignee_id=None, include_closed=False, page_limit=100):
    """Fetch tasks, then manually filter by due_date between [due_start_ms, due_end_ms]."""
    tasks = []
    page = 0
    while True:
        params = {
            "include_closed": "true" if include_closed else "false",
            "subtasks": "true",
            "page": page,
        }
        if assignee_id:
            params["assignees[]"] = str(assignee_id)

        resp = requests.get(f"{API_BASE}/team/{team_id}/task", headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("tasks", [])
        if not batch:
            break

        for t in batch:
            if not t.get("due_date"):
                continue
            try:
                due = int(t["due_date"])
            except Exception:
                continue
            if due_start_ms <= due <= due_end_ms:
                tasks.append(t)

        if len(batch) < page_limit:
            break
        page += 1

    return tasks

def human_label_and_dt(due_ms: int, now_local: datetime, tz: ZoneInfo):
    due_dt = datetime.fromtimestamp(int(due_ms) / 1000, tz)
    delta_days = (due_dt.date() - now_local.date()).days
    if delta_days < 0:
        label = f"overdue ({abs(delta_days)}d)"
    elif delta_days == 0:
        label = "today"
    elif delta_days == 1:
        label = "tomorrow"
    else:
        label = f"in {delta_days} days"
    return label, due_dt

def _is_exam_task(t) -> bool:
    # Uses ClickUp "tags" (not custom field) to detect exams
    tags = t.get("tags", [])
    return any((tg.get("name") or "").lower() == "exam" for tg in tags)

def _within(due_ms: int, end_ms: int) -> bool:
    return due_ms <= end_ms

def _format_task_block(t, now_local: datetime, tz: ZoneInfo):
    label, due_dt = human_label_and_dt(t["due_date"], now_local, tz)
    weekday = due_dt.strftime('%a')  # Mon/Tue/Wed
    name = t.get("name", "(no title)")
    url = t.get("url") or f"https://app.clickup.com/t/{t.get('id')}"
    status = t.get("status", {}).get("status", "unknown")
    tag_list = t.get("tags", [])
    tags_str = " ".join(f"#{tag['name']}" for tag in tag_list) if tag_list else "-"
    icon = "🎓" if _is_exam_task(t) else "📝"
    return (
        f"{icon} {name}\n"
        f"   • Status: {status}\n"
        f"   • Tags: {tags_str}\n"
        f"   • Due: {label} ({due_dt.strftime('%Y-%m-%d')} {weekday})\n"
        f"   • Link: <{url}>"
    )

# ---------- Groq AI summary (optional) ----------
def _short_snapshot(t, now_local, tz):
    label, due_dt = human_label_and_dt(t["due_date"], now_local, tz)
    name = t.get("name", "(no title)")
    status = t.get("status", {}).get("status", "unknown")
    is_exam = "yes" if _is_exam_task(t) else "no"
    return f"- {name} | due: {label} ({due_dt.strftime('%Y-%m-%d')}) | status: {status} | exam: {is_exam}"

def ai_summarize_tasks(tasks, now_local, tz):
    key = os.getenv("GROQ_API_KEY")
    if not key or not tasks:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=key)

        today = now_local.strftime("%Y-%m-%d (%a)")

        # Build compact snapshot
        items = []
        for t in tasks[:40]:  # limit size
            label, due_dt = human_label_and_dt(t["due_date"], now_local, tz)
            name = t.get("name", "(no title)")
            status = t.get("status", {}).get("status", "unknown")
            is_exam = "yes" if _is_exam_task(t) else "no"
            items.append(f"- {name} | due: {label} ({due_dt.strftime('%Y-%m-%d')}) | status: {status} | exam: {is_exam}")
        items_text = "\n".join(items)

        # Language
        lang = (os.getenv("AI_SUMMARY_LANG") or "EN").upper()
        if lang == "TH":
            instructions = (
                "สรุปงานภายใน 7–14 วันเป็น 3–5 bullet "
                "โดยให้ความสำคัญสูงสุดกับงานที่เป็นการสอบ (exam=yes) "
                "จัดลำดับตามความเร่งด่วน แล้วตามด้วยงานอื่น "
                "ปิดท้ายด้วยข้อความให้กำลังใจ 1 บรรทัด"
            )
        else:
            instructions = (
                "Summarize tasks due in the next 7–14 days in 3–5 bullets. "
                "Focus heavily on exam tasks (exam=yes) first, then other urgent work. "
                "Highlight deadlines and risks. End with one short motivational line."
            )

        # Full prompt
        big_paragraph = (
            f"Today: {today}\n"
            f"Tasks list (title | due | status | exam?):\n{items_text}\n\n"
            f"Instructions: {instructions}"
        )

        resp = client.chat.completions.create(
            model="groq/compound",
            messages=[{"role": "user", "content": big_paragraph}],
            temperature=0.4,
            max_tokens=320,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return None

# ---------- Message builder ----------
def build_discord_message(tasks, now_local: datetime, tz: ZoneInfo, days_ahead: int, exam_days_ahead: int):
    if not tasks:
        content = (
            "===================================\n"
            f"📅 Daily Check ({now_local.strftime('%Y-%m-%d')}) (0 works)\n"
            f"- No tasks due soon.\n"
            "==================================="
        )
        return {"content": content}

    # Sort tasks by due date
    tasks_sorted = sorted(tasks, key=lambda t: int(t["due_date"]))

    # Partition: exams vs others (by tag #exam)
    exams = [t for t in tasks_sorted if _is_exam_task(t)]
    others = [t for t in tasks_sorted if not _is_exam_task(t)]

    # Build blocks
    exam_blocks  = [_format_task_block(t, now_local, tz) for t in exams]
    other_blocks = [_format_task_block(t, now_local, tz) for t in others]
    total_count  = len(exam_blocks) + len(other_blocks)

    # --- AI Summary (optional) ---
    ai_summary = ai_summarize_tasks(tasks_sorted, now_local, tz)

    # Compose
    sections = []
    if ai_summary:
        sections.append("🤖 **AI Summary**")
        sections.append(ai_summary)
        sections.append("--------------------------------------")

    sections += [
        f"📚 Upcoming Exams (next {exam_days_ahead} days) — [{len(exam_blocks)} exams]",
        ("\n\n".join(exam_blocks) if exam_blocks else "   • None"),
        "--------------------------------------",
        f"🗓️ Work due Soon (next {days_ahead} days) — [{len(other_blocks)} works]",
        ("\n\n".join(other_blocks) if other_blocks else "   • None"),
    ]
    body = "\n".join(sections).strip()

    text = (
        "===================================\n"
        f"📅 Daily Check ({now_local.strftime('%Y-%m-%d')}) ({total_count} works)\n\n"
        + body +
        "\n==================================="
    )
    return {"content": text}

# ---------- Safe Discord sender ----------
def send_discord_message(webhook, text: str):
    # Split to avoid Discord 2000-char content limit
    MAX_LEN = 1900
    parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    for part in parts:
        resp = requests.post(webhook, json={"content": part}, timeout=30)
        if not (200 <= resp.status_code < 300):
            print(f"Discord webhook failed: {resp.status_code} {resp.text}")
            sys.exit(4)

# ---------- Main ----------
def main():
    load_dotenv()

    token = os.getenv("CLICKUP_TOKEN")
    team_id = os.getenv("CLICKUP_TEAM_ID")
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    days_ahead = int(os.getenv("DAYS_AHEAD", "7"))            # others window
    exam_days_ahead = int(os.getenv("EXAM_DAYS_AHEAD", "14")) # exam window
    only_me = env_bool("ONLY_ASSIGNED_TO_ME", False)
    include_closed = env_bool("INCLUDE_CLOSED", False)

    if not token or not webhook:
        print("ERROR: Please set CLICKUP_TOKEN and DISCORD_WEBHOOK_URL (in environment or .env).")
        sys.exit(2)

    headers = {"Authorization": token}

    if not team_id:
        teams = get_teams(headers)
        if not teams:
            print("ERROR: No Workspaces (teams) found for your token.")
            sys.exit(2)
        print("Please set CLICKUP_TEAM_ID to one of the following IDs and rerun:")
        for t in teams:
            print(f"  {t.get('id')}  -  {t.get('name')}")
        sys.exit(1)

    tz = ZoneInfo("Asia/Bangkok")
    now_local = datetime.now(tz)

    # Start from midnight today
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    # Windows
    end_local_other = (start_local + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)
    end_local_exam  = (start_local + timedelta(days=exam_days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)
    end_local_fetch = end_local_exam  # fetch the larger window once

    start_ms     = int(start_local.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_fetch = int(end_local_fetch.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_other = int(end_local_other.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_exam  = int(end_local_exam.astimezone(timezone.utc).timestamp() * 1000)

    assignee_id = None
    if only_me:
        try:
            assignee_id = get_my_user_id(headers)
        except Exception as e:
            print(f"WARNING: Couldn't get your user id, continuing without assignee filter. Details: {e}")

    # Fetch & window-filter per task type
    try:
        all_tasks = fetch_due_tasks(headers, team_id, start_ms, end_ms_fetch, assignee_id, include_closed)
    except requests.HTTPError as e:
        print("HTTP error from ClickUp:", e.response.status_code, e.response.text)
        sys.exit(3)
    except Exception as e:
        print("Unexpected error fetching tasks:", repr(e))
        sys.exit(3)

    merged = []
    for t in all_tasks:
        try:
            due = int(t["due_date"])
        except Exception:
            continue
        if _is_exam_task(t):
            if _within(due, end_ms_exam):
                merged.append(t)
        else:
            if _within(due, end_ms_other):
                merged.append(t)

    # Build & send
    payload = build_discord_message(merged, now_local, tz, days_ahead, exam_days_ahead)
    send_discord_message(webhook, payload["content"])
    print(f"Sent {len(merged)} task(s) to Discord.")

if __name__ == "__main__":
    main()
