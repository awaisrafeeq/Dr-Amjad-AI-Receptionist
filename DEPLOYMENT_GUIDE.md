# Client Deployment Guide - Hospital Reception Bot

This guide is for completing the deployment of the AI Receptionist application to Azure.

**Pre-requisites (already completed by developer):**
- ✅ Azure Web App created
- ✅ Environment variables configured
- ✅ WebSocket support enabled
- ✅ `DEVTUNNEL_ID` set to: `https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net`

---

## Step 1: Deploy Code to Azure

### Option A: VS Code Extension (Recommended - Easiest)

1. **Install Azure App Service Extension**
   - Open VS Code
   - Go to Extensions (Ctrl+Shift+X)
   - Search: **"Azure App Service"**
   - Click **Install**

2. **Sign in to Azure**
   - Press `Ctrl+Shift+P`
   - Type: **"Azure: Sign In"**
   - Click and sign in with your Azure credentials in the browser

3. **Deploy the App**
   - In VS Code left sidebar, click the **Azure** icon
   - Expand **App Services**
   - Find: **MedCenter-Volta-Calling-Agent**
   - Right-click → **Deploy to Web App**
   - Select the project folder: `azure-ai-caller-main`
   - Click **Deploy**

4. **Wait for deployment** (5-10 minutes)

### Option B: Azure Portal ZIP Upload

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **App Services** → **MedCenter-Volta-Calling-Agent**
3. Click **Deployment** → **Deployment Center**
4. Select **ZIP Deploy**
5. Upload `deployment.zip` file (provided by developer)
6. Click **Deploy**

### Option C: Azure CLI (If installed)

```bash
# Login to Azure
az login

# Deploy the ZIP file
az webapp deployment source config-zip \
  --resource-group rg-Telephone-Agent \
  --name MedCenter-Volta-Calling-Agent \
  --src deployment.zip
```

---

## Step 2: Verify Deployment

After deployment completes, verify the app is running:

1. Go to: `https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net/`

2. Expected response:
   ```json
   {"status": "Hospital Reception Agent is running"}
   ```

3. If you see this message, the deployment is successful!

---

## Step 3: Update ACS Event Grid Webhook

**⚠️ This is critical for receiving phone calls!**

### 3.1 Get Your Webhook URL

Your webhook URL is:
```
https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net/acs/incoming
```

### 3.2 Update Event Grid Topic

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Event Grid Topics**
3. Find: **inboundcall-topic**
4. Click on it
5. Go to **Event Subscriptions** (left menu)
6. Find your existing subscription (e.g., `inboundcall-subscription`)
7. Click on it to open

### 3.3 Update Webhook Endpoint

1. In the Event Subscription details:
   - Look for **"Endpoint Type"**: Select **"Web Hook"**
   - Look for **"Endpoint"** field
2. **Delete the old URL** (if any)
3. **Enter the new URL**:
   ```
   https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net/acs/incoming
   ```
4. Click **Update** or **Save Changes**

### 3.4 Verify Webhook Registration

1. After saving, Azure will send a **validation request** to your webhook
2. If successful, the subscription status will show as **"Active"**
3. If you see errors, check that your app is running (Step 2)

---

## Step 4: Test Incoming Calls

### 4.1 Make a Test Call

1. Call your Azure Communication Services phone number
2. The call should connect to your AI Receptionist
3. You should hear the greeting: *"MedCenter Volta, Sie sprechen mit Kaya..."*

### 4.2 Check Logs (If Issues)

1. Azure Portal → **MedCenter-Volta-Calling-Agent**
2. **Monitoring** → **Log stream**
3. Look for any error messages


## Important URLs

| Purpose | URL |
|---------|-----|
| **App Home** | `https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net/` |
| **ACS Webhook** | `https://medcenter-volta-calling-agent-fsd8de8hgfbugdf8c.wittywater-a051.westeurope-01.azurewebsites.net/acs/incoming` |
| **App Service** | Azure Portal → App Services → MedCenter-Volta-Calling-Agent |
| **Event Grid** | Azure Portal → Event Grid Topics → inboundcall-topic |

---

## Troubleshooting

### Issue: "Failed to deploy"
**Solution**: Check that `deployment.zip` includes all files (especially `startup.sh`)

### Issue: "App returns 500 error"
**Solution**: 
- Check environment variables are set correctly
- Check logs in Azure Portal → Log stream

### Issue: "Event Grid subscription failing"
**Solution**: 
- Ensure app is deployed and running first
- Check webhook URL has `/acs/incoming` at the end

### Issue: "Call not connecting"
**Solution**: 
- Verify ACS phone number is active
- Check Event Grid webhook is configured correctly
- Review Log stream for errors

---

## Contact

If you encounter any issues, please contact the developer with:
1. Screenshot of the error
2. Azure Log stream output
3. Step where you got stuck
