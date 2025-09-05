#!/usr/bin/env python3
# clickup_daily_to_discord.py
# Purpose: Fetch ClickUp tasks and post a summary to Discord.
# - Exams (#exam) -> next EXAM_DAYS_AHEAD days (default 14)
# - Others       -> next DAYS_AHEAD days (default 7)

import os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

API_BASE = "https://api.clickup.com/api/v2"

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
            due = int(t["due_date"])
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
    return (
        f"📝 {name}\n"
        f"   • Status: {status}\n"
        f"   • Tags: {tags_str}\n"
        f"   • Due: {label} ({due_dt.strftime('%Y-%m-%d')} {weekday})\n"
        f"   • Link: <{url}>"
    )

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

    # Count after we build filtered sections (done in main; here we just render lists passed in)
    exam_blocks = []
    other_blocks = []

    # Build blocks
    for t in exams:
        exam_blocks.append(_format_task_block(t, now_local, tz))
    for t in others:
        other_blocks.append(_format_task_block(t, now_local, tz))

    total_count = len(exam_blocks) + len(other_blocks)

    # Compose message with two sections
    sections = [
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

def main():
    load_dotenv()

    token = os.getenv("CLICKUP_TOKEN")
    team_id = os.getenv("CLICKUP_TEAM_ID")
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    days_ahead = int(os.getenv("DAYS_AHEAD", "7"))           # default keep 7
    exam_days_ahead = int(os.getenv("EXAM_DAYS_AHEAD", "14"))# exams use 14
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

    # Compute both windows
    end_local_other = (start_local + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)
    end_local_exam  = (start_local + timedelta(days=exam_days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)

    # Fetch once using the maximum window (exam window), then split in Python
    end_local_fetch = end_local_exam

    start_ms = int(start_local.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_fetch = int(end_local_fetch.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_other = int(end_local_other.astimezone(timezone.utc).timestamp() * 1000)
    end_ms_exam  = int(end_local_exam.astimezone(timezone.utc).timestamp() * 1000)

    assignee_id = None
    if only_me:
        try:
            assignee_id = get_my_user_id(headers)
        except Exception as e:
            print(f"WARNING: Couldn't get your user id, continuing without assignee filter. Details: {e}")

    try:
        all_tasks = fetch_due_tasks(headers, team_id, start_ms, end_ms_fetch, assignee_id, include_closed)
    except requests.HTTPError as e:
        print("HTTP error from ClickUp:", e.response.status_code, e.response.text)
        sys.exit(3)
    except Exception as e:
        print("Unexpected error fetching tasks:", repr(e))
        sys.exit(3)

    # Split by tag and filter by respective window
    exams = []
    others = []
    for t in all_tasks:
        due = int(t["due_date"])
        if _is_exam_task(t):
            if _within(due, end_ms_exam):
                exams.append(t)
        else:
            if _within(due, end_ms_other):
                others.append(t)

    # Build message using the partitioned lists (but build_discord_message expects combined tasks;
    # we’ll temporarily combine after sorting so its internal partitioning matches our filter)
    # Instead, we pass the combined filtered list and let it render sections again
    filtered_tasks = sorted(exams + others, key=lambda x: int(x["due_date"]))

    payload = build_discord_message(filtered_tasks, now_local, tz, days_ahead, exam_days_ahead)

    try:
        resp = requests.post(webhook, json=payload, timeout=30)
        if 200 <= resp.status_code < 300:
            print(f"Sent {len(filtered_tasks)} task(s) to Discord.")
        else:
            print(f"Discord webhook failed: {resp.status_code} {resp.text}")
            sys.exit(4)
    except Exception as e:
        print("Unexpected error sending to Discord:", repr(e))
        sys.exit(4)

if __name__ == "__main__":
    main()
