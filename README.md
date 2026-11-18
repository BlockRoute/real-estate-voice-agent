# Real Estate Voice Agent (Deepgram + Twilio + Cal.com)

Answers inbound calls and calls opted-in leads. Learns motivation, timeline, and pain points, prequalifies buyers (criteria, pre-approval) and sellers (property details), and books an in-person meeting on cal.com/danielyoon. Every call gets logged to `call_log.csv` and, optionally, a webhook.

## Files
- `app.py`: FastAPI server. `/incoming` answers calls, `/call` dials a lead, `/media` bridges Twilio audio to the Deepgram Voice Agent.
- `agent_config.py`: prompt, voice, and the 4 functions the agent can call.
- `functions.py`: Cal.com slots and bookings, SMS confirmations, call logging, hangup.

## Setup
1. **Cal.com:** create two event types: `listing-appointment` (60 min) and `buyer-consultation` (45 min). Add to each the location "In Person (Attendee Address)". Set your hours, buffer, and minimum notice there. Create an API key under Settings > Developer.
2. **Twilio:** in your number's Voice Configuration, set "A call comes in" to Webhook `https://YOUR_HOST/incoming`, HTTP POST. Register it for A2P 10DLC or the confirmation texts won't deliver.
3. **Deepgram:** create an API key.
4. Install and run:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env   # fill it in
   ngrok http 8000        # put the hostname in PUBLIC_HOSTNAME
   uvicorn app:app --port 8000
   ```
5. Call yourself first:
   ```bash
   curl -X POST https://$PUBLIC_HOSTNAME/call \
     -H "Content-Type: application/json" -H "X-API-Key: $CALL_API_KEY" \
     -d '{"to":"+1804XXXXXXX","lead_name":"Jane Smith",
          "property_address":"123 Main St, Midlothian, VA 23113",
          "call_reason":"She requested a home value report on our website."}'
   ```

## Before calling real leads
- **TCPA:** the FCC treats AI voices as "artificial voice," so outbound AI calls need the person's prior express consent. Use this for inbound leads who opted in (website forms, valuation requests), not cold lists. Scrub against the National DNC list and your internal list.
- The agent says it's an AI assistant when asked. Keep that in the prompt.
- If they have an agreement with another agent, the agent bows out without pitching.

## Customizing
- Change the voice: `TTS_MODEL` (`flux-kit-en`, or `aura-2-thalia-en` with `TTS_VERSION=v1`).
- Change the script: edit `build_prompt()` in `agent_config.py`.
- Send results to your CRM: set `LEAD_WEBHOOK_URL` to an n8n or Make webhook. Payload includes the full transcript.

## How the prompt is built
`agent_config.py` follows Deepgram's voice agent prompting guide: speaking-style rules first, spoken-form rules for prices, dates, times, addresses, and emails, a six-stage call flow, explicit function triggers, scripted fallbacks for out-of-scope questions, and an escalation path. Behavior that has to be reliable lives in `app.py` instead of the prompt:
- Filler lines while the calendar loads ("Sure, pulling up Daniel's calendar.")
- Silence handling: after `SILENCE_SECONDS` of quiet it asks "Are you still there?", then hangs up politely and logs a callback
- Calendar times come back from `functions.py` already in spoken form ("Wednesday, November eighteenth at two PM")

Change one prompt section at a time and note it in the changelog at the top of `agent_config.py`.
