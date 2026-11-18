"""Client-side function handlers the voice agent can call.

Calendar: Cal.com API v2 (cal.com/danielyoon). Availability, buffers, minimum
notice, and daily limits all come from your Cal.com event type settings.
"""

import asyncio
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from twilio.rest import Client as TwilioClient

from agent_config import TZ, AGENT_NAME

CAL_API = "https://api.cal.com/v2"
CAL_API_KEY = os.getenv("CAL_API_KEY")  # Cal.com > Settings > Developer > API Keys (starts with cal_)
CAL_USERNAME = os.getenv("CAL_USERNAME", "danielyoon")
CAL_EVENT_SLUGS = {
    "listing_appointment": os.getenv("CAL_LISTING_EVENT_SLUG", "listing-appointment"),
    "buyer_consultation": os.getenv("CAL_BUYER_EVENT_SLUG", "buyer-consultation"),
}
# Event type needs a matching location. "attendeeAddress" = "In Person (Attendee Address)" in Cal.com.
CAL_LOCATION_TYPE = os.getenv("CAL_LOCATION_TYPE", "attendeeAddress")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
LOG_FILE = Path(os.getenv("LEAD_LOG_FILE", "call_log.csv"))
LEAD_WEBHOOK_URL = os.getenv("LEAD_WEBHOOK_URL")  # optional: n8n / Make / CRM
AGENT_CELL = os.getenv("AGENT_CELL_NUMBER")  # you get a text when something books

twilio = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
TWILIO_FROM = os.getenv("TWILIO_PHONE_NUMBER")


def _cal_headers(version: str) -> dict:
    return {"Authorization": f"Bearer {CAL_API_KEY}", "cal-api-version": version}


_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_ORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
        9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
        16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth", 20: "twentieth", 30: "thirtieth"}
_MINUTES = {0: "", 15: " fifteen", 30: " thirty", 45: " forty five"}


def _ordinal(n: int) -> str:
    if n in _ORD:
        return _ORD[n]
    tens = "twenty" if n < 30 else "thirty"
    return f"{tens} {_ORD[n % 10]}"


def _spoken(dt: datetime) -> str:
    """Voice form, so TTS never reads digits: Wednesday, November eighteenth at two thirty PM."""
    hour = _ONES[dt.hour % 12 or 12]
    minute = _MINUTES.get(dt.minute, f" {dt.minute:02d}")
    return f"{dt.strftime('%A, %B')} {_ordinal(dt.day)} at {hour}{minute} {dt.strftime('%p')}"


def _written(dt: datetime) -> str:
    """Text-message form: Wed, Nov 18 at 2:30 PM."""
    return f"{dt.strftime('%a, %b')} {dt.day} at {dt.strftime('%I:%M %p').lstrip('0')}"


def _pick_spread(slots: list[datetime], n: int = 6) -> list[datetime]:
    """First open slot per day, then fill. Gives the seller real choices, not 10:00/10:30/11:00."""
    seen, firsts, rest = set(), [], []
    for s in slots:
        (rest if s.date() in seen else firsts).append(s)
        seen.add(s.date())
    return sorted((firsts + rest)[:n])


async def check_availability(args: dict, ctx: dict) -> dict:
    now = datetime.now(TZ)
    preferred = args.get("preferred_date")
    if preferred:
        start, end = preferred, preferred
    else:
        start, end = now.date().isoformat(), (now + timedelta(days=DAYS_AHEAD)).date().isoformat()

    params = {
        "username": CAL_USERNAME,
        "eventTypeSlug": CAL_EVENT_SLUGS[args.get("meeting_type", "listing_appointment")],
        "start": start,
        "end": end,
        "timeZone": str(TZ),
    }
    async with httpx.AsyncClient(timeout=6) as client:
        r = await client.get(f"{CAL_API}/slots", params=params, headers=_cal_headers("2024-09-04"))
    r.raise_for_status()

    times = []
    for day_slots in r.json().get("data", {}).values():
        for s in day_slots:
            iso = s["start"] if isinstance(s, dict) else s
            times.append(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ))

    ranges = {"morning": (0, 12), "afternoon": (12, 16), "evening": (16, 24)}
    lo, hi = ranges.get(args.get("time_of_day", "any"), (0, 24))
    times = sorted(t for t in times if lo <= t.hour < hi)

    if not times:
        return {"slots": [], "message": "No openings in that window. Ask for another day."}
    return {
        "slots": [{"start_iso": t.isoformat(), "spoken": _spoken(t)} for t in _pick_spread(times)],
        "message": "Offer two of these, on different days if possible.",
    }


async def _sms(to: str, body: str) -> None:
    # A failed text must never undo a booking that already went through.
    try:
        await asyncio.to_thread(twilio.messages.create, to=to, from_=TWILIO_FROM, body=body)
    except Exception as e:
        print(f"[sms] to {to} failed: {e}")


async def book_appointment(args: dict, ctx: dict) -> dict:
    start_local = datetime.fromisoformat(args["start_iso"]).astimezone(TZ)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lead_phone = ctx.get("lead_phone", "")
    location = args["meeting_location"]
    meeting_type = args.get("meeting_type", "listing_appointment")
    label = "listing appointment" if meeting_type == "listing_appointment" else "buyer consultation"

    attendee = {"name": args.get("lead_name") or "Homeowner", "timeZone": str(TZ), "language": "en"}
    if args.get("lead_email"):
        attendee["email"] = args["lead_email"]
    if lead_phone:
        attendee["phoneNumber"] = lead_phone

    body = {
        "start": start_utc,
        "username": CAL_USERNAME,
        "eventTypeSlug": CAL_EVENT_SLUGS[meeting_type],
        "attendee": attendee,
        "location": {"type": CAL_LOCATION_TYPE, "address": location},
        "metadata": {
            "source": "ai_voice_agent",
            "meeting_location": location[:500],
            "call_sid": ctx.get("call_sid", ""),
        },
    }
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.post(f"{CAL_API}/bookings", json=body, headers=_cal_headers("2026-02-25"))

    if r.status_code >= 400:
        print(f"[cal.com] booking failed {r.status_code}: {r.text}")
        if TWILIO_FROM and AGENT_CELL:  # the agent promises Daniel will text them, so make sure he knows
            await _sms(AGENT_CELL, f"BOOKING FAILED, text them to confirm: {args.get('lead_name')} | {location} | "
                                   f"{_written(start_local)} | {lead_phone}")
        # Don't retry: a config error fails every slot, and "that time just booked" misleads the caller.
        return {
            "booked": False,
            "message": "The booking did not go through because of a system error, not because the time was taken. "
            "Don't offer other times and don't say the slot was booked. Say you weren't able to lock it in on "
            "your end and Daniel will text them shortly to confirm that time, then log call_back_later with the "
            "requested time in callback_time.",
        }

    booking = r.json().get("data", {})
    when = _spoken(start_local)
    when_text = _written(start_local)
    ctx["booked_when"] = when_text

    # Cal.com emails the lead only if we have their email, so text them too.
    if TWILIO_FROM and lead_phone:
        await _sms(lead_phone, f"Confirmed: your {label} with {AGENT_NAME} at {location} on {when_text}. "
                               "Reply here if anything changes.")
    if TWILIO_FROM and AGENT_CELL:
        await _sms(AGENT_CELL, f"NEW {label.upper()}: {args.get('lead_name')} | {location} | {when_text} | {lead_phone}")
    return {"booked": True, "booking_uid": booking.get("uid"), "when": when}


def _cell_safe(v):
    # Values come from what callers say. A cell starting with = @ + - opens as a formula in Excel and
    # Google Sheets (the sheet webhook gets this row too). Phone numbers keep their leading "+".
    s = "" if v is None else str(v)
    if s[:1] in ("=", "@", "\t", "\r") or (s[:1] in ("+", "-") and not s[1:].replace(" ", "").isdigit()):
        return "'" + s
    return s


async def log_outcome(args: dict, ctx: dict) -> dict:
    fields = ["outcome", "lead_type", "motivation", "timeline", "pain_points", "property_details",
              "buyer_criteria", "preapproval", "preapproval_details", "callback_time", "notes"]
    row = {
        "timestamp": datetime.now(TZ).isoformat(),
        "call_sid": ctx.get("call_sid", ""),
        "direction": ctx.get("direction", ""),
        "lead_name": ctx.get("lead_name", ""),
        "lead_phone": ctx.get("lead_phone", ""),
        "property_on_file": ctx.get("property_address", ""),
        "booked_when": ctx.get("booked_when", ""),
        **{f: args.get(f, "") for f in fields},
    }
    row = {k: _cell_safe(v) for k, v in row.items()}
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if new_file:
            writer.writeheader()
        writer.writerow(row)

    if LEAD_WEBHOOK_URL:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(LEAD_WEBHOOK_URL, json={**row, "transcript": ctx.get("transcript", [])})
        except httpx.HTTPError as e:
            print(f"[webhook] failed: {e}")

    ctx["logged"] = True
    return {"logged": True}


async def end_call(args: dict, ctx: dict) -> dict:
    # Actual hangup happens in app.py once the goodbye audio finishes playing.
    ctx["hangup_pending"] = True
    return {"ok": True}


HANDLERS = {
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "log_outcome": log_outcome,
    "end_call": end_call,
}


async def run_function(name: str, raw_args: str, ctx: dict) -> str:
    handler = HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"unknown function {name}"})
    try:
        args = json.loads(raw_args) if raw_args else {}
        # Never leave the agent hanging: Deepgram waits forever for a response.
        result = await asyncio.wait_for(handler(args, ctx), timeout=10)
    except asyncio.TimeoutError:
        result = {"error": "The calendar is slow right now. Offer to have Daniel text them times."}
    except Exception as e:  # noqa: BLE001
        print(f"[function:{name}] error: {e}")
        result = {"error": "Something went wrong. Offer to have Daniel follow up by text."}
    return json.dumps(result)
