# Azure Deployment Guide - Hospital Reception Bot

Complete guide for deploying the AI Receptionist project to Azure App Service.

---

## Prerequisites

1. **Azure CLI** installed: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli
2. **Azure account** with active subscription
3. **Git** repository (GitHub recommended)
4. All Azure services created (from previous setup):
   - Azure OpenAI
   - Azure Communication Services
   - Azure AI Search
   - Azure Blob Storage
   - Azure Cosmos DB
   - Event Grid Topic

---

## Step 1: Azure App Service Create Karna

### Option A: Azure Portal se (Easy way)

1. [Azure Portal](https://portal.azure.com) open karein
2. **"App Services"** search karein aur click karein
3. **+ Create** button click karein
4. Settings:
   - **Subscription:** Aapki subscription select karein
   - **Resource Group:** `rg-Telephone-Agent` (existing select karein)
   - **Name:** `hospital-reception-bot` (ya koi unique name)
   - **Publish:** Code
   - **Runtime stack:** Python 3.11
   - **Operating System:** Linux (Recommended)
   - **Region:** Same as your other services (e.g., West Europe)
   - **Pricing Plan:** Basic B1 (minimum for WebSocket support)

5. **Review + create** → **Create**

### Option B: Azure CLI se

```bash
# Login to Azure
az login

# Set subscription (agar multiple hain)
az account set --subscription "Your-Subscription-Name"

# Create App Service Plan (Linux)
az appservice plan create \
  --name reception-bot-plan \
  --resource-group rg-Telephone-Agent \
  --sku B1 \
  --is-linux

# Create Web App
az webapp create \
  --name hospital-reception-bot \
  --resource-group rg-Telephone-Agent \
  --plan reception-bot-plan \
  --runtime "PYTHON:3.11" \
  --startup-file "startup.sh"
```

---

## Step 2: Environment Variables Configure Karna

### Azure Portal mein:

1. App Service → **Settings** → **Configuration**
2. **+ New application setting** for each variable:

**Required Environment Variables:**

| Setting Name | Value (Aapki .env se copy karein) |
|--------------|-----------------------------------|
| `AZURE_OPENAI_ENDPOINT` | https://your-resource.openai.azure.com/ |
| `AZURE_OPENAI_KEY` | Your OpenAI API Key |
| `AZURE_OPENAI_DEPLOYMENT` | gpt-4o-realtime |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | text-embedding-3-small |
| `AZURE_OPENAI_API_VERSION` | 2024-10-01-preview |
| `ACS_CONNECTION_STRING` | endpoint=https://... |
| `ACS_SOURCE_NUMBER` | +4179... |
| `COGNITIVE_SERVICE_ENDPOINT` | https://...cognitiveservices.azure.com |
| `DEVTUNNEL_ID` | https://your-app.azurewebsites.net |
| `AZURE_BLOB_CONN` | DefaultEndpointsProtocol=https... |
| `AZURE_SEARCH_ENDPOINT` | https://your-search.search.windows.net |
| `AZURE_SEARCH_KEY` | Your Search Key |
| `AZURE_COSMOS_URI` | https://your-cosmos.documents.azure.com |
| `AZURE_COSMOS_KEY` | Your Cosmos Key |
| `AZURE_COSMOS_DB_NAME` | call-logs-db |
| `EPAAD_BASE_URL` | https://mcv.epaad.ch |
| `EPAAD_USERNAME` | Your EPAAD username |
| `EPAAD_PASSWORD` | Your EPAAD password |

**Important Settings:**
- `WEBSITES_PORT` = `8000` (Default port for uvicorn)
- `SCM_DO_BUILD_DURING_DEPLOYMENT` = `true`

3. **Save** button click karein (App restart hoga)

---

## Step 3: WebSocket Enable Karna

WebSocket support zaroori hai audio streaming ke liye:

1. App Service → **Settings** → **Configuration** → **General settings**
2. **Web sockets** → **On**
3. **Save**

---

## Step 4: Code Deploy Karna

### Method 1: VS Code Extension (Recommended for beginners)

1. **VS Code** mein Azure App Service extension install karein
2. VS Code mein `Ctrl+Shift+P` → "Azure: Sign In"
3. Left sidebar mein Azure icon click karein
4. App Services → Aapka app right-click → **Deploy to Web App**
5. Local folder select karein (project root)
6. Confirm deployment

### Method 2: Azure CLI se

```bash
# Project folder mein
az webapp deployment source config-zip \
  --resource-group rg-Telephone-Agent \
  --name hospital-reception-bot \
  --src deployment.zip
```

**Pehle zip create karein:**
```bash
# Windows PowerShell mein:
Compress-Archive -Path * -DestinationPath deployment.zip -Force
# (venv aur logs folder exclude karein)
```

### Method 3: GitHub Actions (Auto-deployment)

1. `.github/workflows/azure-deploy.yml` already created hai
2. GitHub repository mein jayein: **Settings** → **Secrets and variables** → **Actions**
3. **New repository secret**:
   - Name: `AZURE_WEBAPP_PUBLISH_PROFILE`
   - Value: Azure Portal se download karein (see below)

**Publish Profile download kaise karein:**
- Azure Portal → App Service → **Overview** → **Get publish profile**
- File download hogi, uska content copy karein

4. GitHub Actions mein `AZURE_WEBAPP_NAME` bhi set karein ya workflow file mein update karein

---

## Step 5: ACS Callback URL Update Karna

Azure deploy hone ke baad, aapki app ka URL change ho jayega:

1. App Service → **Overview** → URL copy karein (e.g., `https://hospital-reception-bot.azurewebsites.net`)
2. **Event Grid Topic** mein subscription update karein:
   - Azure Portal → Event Grid Topics → `inboundcall-topic`
   - Event Subscription → Webhook URL update karein:
     ```
     https://hospital-reception-bot.azurewebsites.net/acs/incoming
     ```

3. **Environment Variable** update karein:
   - `DEVTUNNEL_ID` ko new URL se replace karein:
     ```
     https://hospital-reception-bot.azurewebsites.net
     ```

---

## Step 6: Deployment Verify Karna

### Health Check:

Browser mein open karein:
```
https://hospital-reception-bot.azurewebsites.net/
```

Response expected:
```json
{"status": "Hospital Reception Agent is running"}
```

### Logs Check Karne Ke Liye:

1. Azure Portal → App Service → **Monitoring** → **Log stream**
2. Ya CLI se:
   ```bash
   az webapp log tail --name hospital-reception-bot --resource-group rg-Telephone-Agent
   ```

---

## Step 7: CORS Configuration (Agar Frontend Alag Hai)

Agar aapka frontend alag domain par hai:

1. App Service → **API** → **CORS**
2. Allowed origins mein add karein:
   - `https://your-frontend-domain.com`
   - `http://localhost:3000` (for local testing)

---

## Troubleshooting

### Common Issues:

**1. App Not Starting:**
```
Logs check karein: Log stream ya Application Insights
Most likely: Environment variables missing
```

**2. WebSocket Connection Fail:**
```
1. App Service → Configuration → Web sockets: ON
2. Pricing tier check karein (Free tier mein WebSocket limited hai)
```

**3. Import/Module Errors:**
```
requirements.txt sahi hai check karein
Azure mein Python version match karein (3.11)
```

**4. Environment Variables Not Loading:**
```
App Service restart karein (Overview → Restart)
```

### Useful Commands:

```bash
# App restart
az webapp restart --name hospital-reception-bot --resource-group rg-Telephone-Agent

# SSH into container
az webapp ssh --name hospital-reception-bot --resource-group rg-Telephone-Agent

# Deployment logs
az webapp deployment log tail --name hospital-reception-bot --resource-group rg-Telephone-Agent
```

---

## Post-Deployment: Testing

### API Endpoints Test Karein:

1. **Health Check:**
   ```bash
   curl https://hospital-reception-bot.azurewebsites.net/
   ```

2. **Appointments API:**
   ```bash
   curl https://hospital-reception-bot.azurewebsites.net/appointments/doctors
   ```

3. **Call Events (ACS):**
   Event Grid webhook test via Azure Portal

---

## Important Notes

1. **Always Free tier avoid karein** - WebSocket aur audio streaming ke liye minimum Basic B1 chahiye
2. **DevTunnel ID** deploy ke baad update karna mat bhoolna
3. **Event Grid webhook** URL bhi update karni hai
4. **Logs** enable karein for debugging

---

## Next Steps

1. **Custom domain** configure karein (optional)
2. **SSL certificate** (auto-enabled for azurewebsites.net)
3. **Application Insights** enable karein for monitoring
4. **Auto-scaling** configure karein (if high traffic expected)

---

Koi issue aye to pehle **Log Stream** check karein - wahan usually sab pata chal jata hai!
