# Project Status (Very Simple English)

## 1) What this project does
This project is an **AI phone receptionist**.

When someone calls your **ACS phone number**, the bot should:
- Answer the call
- Listen to the caller
- Talk back (voice)
- Save call logs to Cosmos DB

## 2) What services are used
- **Azure Communication Services (ACS)**
  - Gets the real phone call (PSTN)
  - Sends call events to your webhooks
  - Streams live audio by WebSocket

- **Azure OpenAI Realtime**
  - Understands the caller audio
  - Creates the bot response
  - Sends audio back in real time

- **Azure Cosmos DB**
  - Stores call metadata and session logs

- **Public URL (ngrok / dev tunnel)**
  - Makes your local FastAPI server reachable from the internet

## 3) Main code files (where the important logic is)
- `app.py`
  - Starts the FastAPI server

- `routers/acs_call_events_routes.py`
  - `POST /acs/incoming` (new call)
  - `POST /acs/callbacks/{contextId}` (call events)
  - `WS /realtime-acs` (audio streaming)

- `utils/acs.py`
  - Answers the call with `CallAutomationClient.answer_call()`
  - Sets media streaming to your `wss://.../realtime-acs`

- `utils/rtmt.py`
  - Bridges audio between ACS WebSocket and OpenAI Realtime WebSocket

- `utils/helpers.py`
  - Converts message formats:
    - ACS -> OpenAI
    - OpenAI -> ACS

- `utils/session_manager.py` + `utils/azure_storage_logger.py`
  - Create sessions and store logs in Cosmos DB

## 4) Call flow (step by step)
1. Caller calls your ACS phone number.
2. ACS sends event to `POST /acs/incoming`.
3. Your app answers the call.
4. ACS sends `CallConnected` and `MediaStreamingStarted` to `POST /acs/callbacks/...`.
5. ACS opens WebSocket `WS /realtime-acs` and starts sending audio.
6. Your app connects to Azure OpenAI Realtime.
7. Your app forwards:
   - Caller audio -> OpenAI
   - Bot audio -> ACS

## 5) What we already fixed
- **Webhook stability**
  - `/acs/incoming` now returns `200 OK` even on internal errors (prevents ACS retries / call failures).

- **Cognitive Services endpoint validation**
  - Only passes `cognitive_services_endpoint` if it looks correct.

- **OpenAI Realtime required header**
  - Added `OpenAI-Beta: realtime=v1`.

- **VAD / StopAudio change**
  - We stopped sending `StopAudio` to ACS on VAD because it can interrupt the stream.

- **Audio session settings**
  - Added `modalities` + `input_audio_format` + `output_audio_format` in `session.update`.

- **No auto-reload during calls**
  - `reload=False` in `app.py`.

- **Bot can greet on pickup**
  - After OpenAI Realtime connects, the app sends an initial `response.create` to trigger a greeting.

## 6) Current issues (still work to do)
- **Streaming can still drop**
  - Sometimes OpenAI closes with code `1006` and ACS shows `streamConnectionInterrupted`.
  - Next step is to verify audio format compatibility and add more safety.

- **Session warning in logs**
  - You may see: `Could not resolve session for event logging`.
  - This happens when `session_id` is missing or not mapped correctly.

- **Network/DNS errors sometimes happen**
  - We still need retry/backoff for some Azure calls.

## 7) How to run (simple)
1. Run the server: `python app.py`
2. Start ngrok: `ngrok http 8001`
3. Put the public URL in ACS/Event Grid callbacks.
4. Call the ACS number and check logs.
