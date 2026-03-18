# Next Steps (What to do next)

This file lists the next work items for this project.

## 1) Use real doctor information (not random)
Right now the bot is told in `system_prompt.md` to “make up” doctor info.

Recommended doctor data fields (simple):
- Doctor name
- Specialty
- Available days/hours
- Location/department
- Appointment phone/extension

Options:
- Put a real doctor list directly into the prompt (fastest)
  - Good for small lists
  - Downside: editing requires updating the prompt and restarting

- Store doctors in a simple file (JSON/CSV) and load it in the app
  - Good for medium lists
  - You can update the file without changing AI behavior too much

- Upload doctor CSV/PDF and use Azure AI Search retrieval (best for scaling)
  - Best for large hospitals and frequent updates
  - The bot will answer from your indexed data instead of making things up

## 2) User experience improvements
Tasks:
- Confirm bot greeting happens immediately on call pickup.
- Add short fallback messages if transcription is empty or noisy.
- Add safe “I can’t hear you, please repeat” handling.

## 3) Deployment / operations
Tasks:
- Host the FastAPI app on Azure (stable public HTTPS URL)
  - Example hosting choices:
    - Azure App Service (FastAPI + Uvicorn)
    - Azure Container Apps (container-based)
  - Then set ACS/Event Grid callbacks to your Azure domain (no tunnels)

- Add a simple run guide (local dev vs production)
  - Local dev: run server + temporary public URL (only for testing)
  - Production: Azure URL only

- Add health checks and minimal monitoring.
