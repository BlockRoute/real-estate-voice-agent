"""Real estate voice agent: Twilio <-> Deepgram Voice Agent API bridge.

Inbound:  point your Twilio number's "A call comes in" webhook at https://YOUR_HOST/incoming (POST).
Outbound: POST /call with your CALL_API_KEY header (see README).
"""

import asyncio
import base64
import json
import os
import re
from xml.sax.saxutils import quoteattr

import websockets
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

from agent_config import build_settings  # noqa: E402
from functions import run_function, twilio, TWILIO_FROM  # noqa: E402

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
DEEPGRAM_AGENT_URL = os.getenv("DEEPGRAM_AGENT_URL", "wss://agent.deepgram.com/v1/agent/converse")
PUBLIC_HOSTNAME = os.environ["PUBLIC_HOSTNAME"]  # e.g. abc123.ngrok-free.app (no https://)
CALL_API_KEY = os.environ["CALL_API_KEY"]  # protects /call so nobody else can dial out on your account
validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])

# Spoken while a slow function runs, injected by the server (more reliable than the LLM).
# Keep these out of the prompt's "don't say" phrases.
FILLERS = {
    "check_availability": "Sure, pulling up Daniel's calendar.",
    "book_appointment": "Perfect, getting that on the calendar.",
}
SILENCE_SECONDS = float(os.getenv("SILENCE_SECONDS", "12"))
# Call screeners (iPhone, Pixel) say "please stay on the line" and then go quiet while the phone rings.
HOLD_SECONDS = float(os.getenv("HOLD_SECONDS", "45"))
HOLD_PHRASES = re.compile(r"stay on the line|please hold|hold on|see if (this person|they)('s| is)? available",
                          re.IGNORECASE)

app = FastAPI()


def stream_twiml(params: dict) -> str:
    param_xml = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v or '')} />" for k, v in params.items()
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f'<Stream url="wss://{PUBLIC_HOSTNAME}/media">{param_xml}</Stream>'
        f"</Connect></Response>"
    )


@app.get("/")
async def health():
    return {"ok": True}


class CallRequest(BaseModel):
    to: str
    lead_name: str = ""
    property_address: str = ""
    call_reason: str = ""


@app.post("/call")
async def start_call(body: CallRequest, request: Request):
    if request.headers.get("X-API-Key") != CALL_API_KEY:
        raise HTTPException(status_code=401)
    # Twilio maps letters to keypad digits, so a placeholder like +1804XXXXXXX dials a real number.
    if not re.fullmatch(r"\+1\d{10}", body.to):
        raise HTTPException(status_code=422, detail="'to' must be +1 followed by 10 digits, e.g. +18045551234")
    twiml = stream_twiml(
        {
            "direction": "outbound",
            "lead_name": body.lead_name,
            "lead_phone": body.to,
            "property_address": body.property_address,
            "call_reason": body.call_reason,
        }
    )
    call = twilio.calls.create(
        to=body.to,
        from_=TWILIO_FROM,
        twiml=twiml,
        status_callback=f"https://{PUBLIC_HOSTNAME}/status",
        status_callback_event=["completed"],
    )
    return {"call_sid": call.sid}


@app.post("/incoming")
async def incoming(request: Request):
    form = dict(await request.form())
    url = f"https://{PUBLIC_HOSTNAME}/incoming"
    if not validator.validate(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403)
    twiml = stream_twiml(
        {
            "direction": "inbound",
            "lead_name": "",
            "lead_phone": form.get("From", ""),
            "property_address": "",
            "call_reason": "They called our number.",
        }
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/status")
async def call_status(request: Request):
    form = await request.form()
    print(f"[status] {form.get('CallSid')} -> {form.get('CallStatus')} ({form.get('CallDuration')}s)")
    return Response(status_code=204)


@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    ctx: dict = {"transcript": []}
    stream_sid = None
    dg = None
    tasks: list[asyncio.Task] = []
    silence = {"task": None, "strikes": 0, "on_hold": False}

    def cancel_silence():
        if silence["task"]:
            silence["task"].cancel()
            silence["task"] = None

    async def silence_watch():
        # Enforced in code, not the prompt: check in once, then hang up if nobody answers.
        await asyncio.sleep(HOLD_SECONDS if silence["on_hold"] else SILENCE_SECONDS)
        silence["strikes"] += 1
        if silence["strikes"] == 1:
            await dg.send(json.dumps({"type": "InjectAgentMessage", "behavior": "default",
                                      "message": "Are you still there?"}))
        else:
            ctx["hangup_pending"] = True
            await dg.send(json.dumps({"type": "InjectAgentMessage", "behavior": "default",
                                      "message": "Sounds like I lost you. We'll try you again at a later time. Bye now."}))

    async def dg_to_twilio():
        async for message in dg:
            if isinstance(message, bytes):
                payload = base64.b64encode(message).decode()
                await ws.send_text(
                    json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                )
                continue

            event = json.loads(message)
            etype = event.get("type")

            if etype == "ConversationText":
                ctx["transcript"].append({"role": event["role"], "content": event["content"]})
                print(f"[{event['role']}] {event['content']}")
                if event["role"] == "user":
                    # Set per user turn, so the wait goes back to normal once a real person speaks.
                    silence["on_hold"] = bool(HOLD_PHRASES.search(event["content"]))
                    if silence["on_hold"]:
                        print(f"[silence] hold detected, waiting up to {HOLD_SECONDS:.0f}s")

            elif etype == "UserStartedSpeaking":
                # Barge-in: drop audio Twilio has buffered but not yet played.
                await ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                cancel_silence()
                silence["strikes"] = 0

            elif etype in ("AgentThinking", "AgentStartedSpeaking"):
                cancel_silence()

            elif etype == "FunctionCallRequest":
                cancel_silence()
                for fn in event.get("functions", []):
                    if not fn.get("client_side", True):
                        continue
                    print(f"[function] {fn['name']}({fn.get('arguments')})")
                    if fn["name"] in FILLERS:
                        await dg.send(json.dumps({"type": "InjectAgentMessage", "behavior": "queue",
                                                  "message": FILLERS[fn["name"]]}))
                    content = await run_function(fn["name"], fn.get("arguments", "{}"), ctx)
                    await dg.send(
                        json.dumps(
                            {"type": "FunctionCallResponse", "id": fn["id"], "name": fn["name"], "content": content}
                        )
                    )

            elif etype == "AgentAudioDone":
                if ctx.get("hangup_pending"):
                    # Ask Twilio to tell us when the goodbye has actually finished playing.
                    await ws.send_text(
                        json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": "hangup"}})
                    )
                else:
                    cancel_silence()
                    silence["task"] = asyncio.create_task(silence_watch())

            elif etype == "InjectionRefused":
                pass  # person started talking; no filler needed

            elif etype in ("Error", "Warning"):
                print(f"[deepgram {etype}] {event}")

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")

            if ev == "start":
                start = msg["start"]
                stream_sid = start["streamSid"]
                ctx.update(start.get("customParameters", {}))
                ctx["call_sid"] = start.get("callSid", "")
                print(f"[call] {ctx.get('direction')} {ctx.get('lead_phone')} ({ctx['call_sid']})")

                dg = await websockets.connect(
                    DEEPGRAM_AGENT_URL,
                    additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                )
                welcome = json.loads(await dg.recv())
                if welcome.get("type") != "Welcome":
                    print(f"[deepgram] unexpected first message: {welcome}")
                settings = build_settings(
                    ctx.get("direction", "outbound"),
                    ctx.get("lead_name", ""),
                    ctx.get("property_address", ""),
                    ctx.get("call_reason", ""),
                )
                print(f"[settings] llm={settings['agent']['think']['provider']['model']} "
                      f"voice={settings['agent']['speak']['provider']['model']}")
                await dg.send(json.dumps(settings))
                tasks.append(asyncio.create_task(dg_to_twilio()))

            elif ev == "media" and dg is not None:
                await dg.send(base64.b64decode(msg["media"]["payload"]))

            elif ev == "mark" and msg.get("mark", {}).get("name") == "hangup":
                twilio.calls(ctx["call_sid"]).update(status="completed")
                break

            elif ev == "stop":
                break

    except WebSocketDisconnect:
        pass
    finally:
        # Safety net: every call gets logged even if the agent skipped it.
        if not ctx.get("logged") and ctx.get("call_sid"):
            dropped = silence["strikes"] >= 2
            await run_function(
                "log_outcome",
                json.dumps({"outcome": "call_back_later" if dropped else "no_outcome", "lead_type": "unknown",
                            "notes": "No response, call dropped by silence timeout." if dropped
                            else "Call ended before the agent logged an outcome."}),
                ctx,
            )
        cancel_silence()
        for t in tasks:
            t.cancel()
        if dg is not None:
            await dg.close()
