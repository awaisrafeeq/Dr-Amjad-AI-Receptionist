# Here's a detailed summary of what this project

Here's a detailed summary of what this project does, which third-party services (especially Azure services) it uses, and the purpose of each component:

---

## **Project Overview:**

Name: Hospital Reception Bot (see app title)\<br\>General Description:This project is an AI backend service — built with FastAPI — serving as the intelligence/core for a hospital reception assistant bot. Its main duties include handling incoming phone calls (using Azure Communication Services), processing documents (PDF/CSV uploads, AI search), storing and analyzing call/session/transcription data, and offering seamless integration with Azure AI (OpenAI), Cosmos DB, Blob Storage, and AI Search services.

---

## **Key 3rd-Party & Azure Services Used:**

### **1\. Azure Communication Services (ACS)**

* Purpose: Manages real-time phone call events, media streaming (audio), call answering, and callback lifecycle for hospital reception scenarios.  
* Usage:  
* Accepts and manages inbound phone calls.  
* Streams and processes audio in real-time.  
* Receives callbacks/events about call lifecycle (connected, disconnected, participants updated, etc.).

### **2\. Azure OpenAI**

* Purpose: Provides AI models for tasks like embedding creation (turning chunks of document text into vectors for search) and possibly generative AI for call understanding or chatbot features.  
* Usage:  
* Generates vector embeddings for document content.  
* May also be used for conversation, transcription analysis, or other generative AI tasks (based on keys/config).

### **3\. Azure Blob Storage**

* Purpose: Secure storage for uploaded documents (PDF/CSV).  
* Usage:  
* Stores files uploaded via the /documents/upload endpoint, making them available for further analysis or indexing.

### **4\. Azure Cognitive Search (Azure AI Search)**

* Purpose: Power document and knowledge base search (vector, text search, filter, etc.).  
* Usage:  
* Chunks and indexes uploaded documents.  
* Embeds vectorized content from OpenAI.  
* Supports fast, semantic querying for downstream knowledge-bot or retrieval-augmented-generation.

### **5\. Azure Cosmos DB**

* Purpose: Stores all operational records, logs, call/session metadata, transcriptions, analytics, etc., in globally-distributed, scalable NoSQL storage.  
* Usage:  
* Stores call/session logs, metadata, transcriptions, call histories.  
* Enables analytics, compliance, audit trails, etc.

---

## **Other 3rd-party Libraries**

* openai: Used to interact with Azure OpenAI (\<u\>not\</u\> the public OpenAI API, configured for Azure).  
* PyPDF2, pandas: For PDF/CSV processing.  
* aiohttp, websockets: For async HTTP and WebSocket communication.  
* fastapi, uvicorn: Web server and API framework.  
* python-dotenv: Environment variable/config loading.

---

## **Feature Summary & Purpose Breakdown:**

### **Inbound & Real-Time Call Handling**

* Receives and manages phone calls using Azure Communication Services.  
* Can process audio stream in real time, potentially to feed to AI models or analytics subsystems.  
* Handles all ACS event lifecycles (participants, media stream start/stop, speech synthesis, call disconnect, etc.).  
* Offers a websocket handler to pipe audio data elsewhere — likely for OpenAI-based AI audio processing.

### **Document Knowledge Management**

* Users can upload documents (CSV, PDF) — these are split, embedded via AI, and indexed in Azure Cognitive Search (with blob backup in storage).  
* Allows AI-powered search over uploaded documents (semantic and vector search).

### **Logging & Analytics**

* All core events (session starts, call events, transcriptions, etc.) are logged in Cosmos DB, with robust error handling.  
* Enables analytics over call performance, compliance, user interactions, and operational auditability.

### **Configurable/Cloud-Ready**

* All major cloud integrations (keys, endpoints) are loaded from environment/config (.env) for secure, environment-specific deployment.

---

## **Example Flows:**

1. A patient calls the hospital number:  
* ACS receives/answers the call.  
* Media is streamed and events are logged.  
* Audio, session, and call logs are persisted to Cosmos DB.  
* (Optionally) AI listens/understands call and assists/interacts.  
1. A staff member uploads a PDF or CSV:  
* File lands in Blob Storage.  
* Document is parsed, chunked, AI-embedded (Azure OpenAI), and indexed in Cognitive Search.  
* Fast, semantic document search and retrieval is now possible for staff and AI assistants.

---

## **Summary Table of Azure Services**

| Service | Purpose |
| :---- | :---- |
| Azure ACS | Phone call handling, streaming, events |
| Azure OpenAI | Text embedding, AI/NLP tasks |
| Blob Storage | File uploads (PDF/CSV) |
| Cosmos DB | All operational/call/log storage |
| Cognitive Search | Advanced knowledge/document search |

---

If you want a diagram, user flow, or have any questions about specific files or code, let me know\!

# Given your requirements and Azure resource

Given your requirements and Azure resource inventory, here’s a detailed deployment and integration roadmap tailored to your client's needs (Swiss-number-only, SIP trunk integration, Ribbon SBC, and your existing Azure stack):

---

## **Step 1: Azure Communication Services (ACS) Setup**

Your resource acs-Telephone-Agent (Communication Service) is the cloud telephony foundation. ACS can connect to PSTN via Direct Routing/SIP Trunk using a certified SBC like Ribbon.Key actions ahead:

* Restrict inbound calls to Swiss numbers:  
* This is enforced primarily at the SIP trunk provider level (Peoplefone), but you should also add logic in your application to ignore/filter non-Swiss calls at the API/event-processing level for extra safety.  
* Integrate SIP Trunk (Peoplefone):  
* Order a Swiss DID from Peoplefone.  
* Configure Peoplefone to route calls to your Ribbon SBC public IP.

---

## **Step 2: Ribbon SBC (Session Border Controller) Configuration**

You need to set up and configure Ribbon's SBC (hosted on Azure or on-prem). It sits between Peoplefone and Azure ACS:

1. Obtain HW ID: Send the HW ID of your Ribbon SBC VM to Nuvola for licensing (as per your email with Gareth).  
1. Licensing: Wait for them to provide a temporary (and later, full) license.  
1. Configure SIP Trunk:  
* Work with Peoplefone to send calls to the public IP of Ribbon SBC.  
* Ribbon SBC should then be configured to route calls to Azure Communication Services (using ACS Direct Routing).

References:

* Ribbon SBC Direct Routing for Microsoft Teams/ACS \- Microsoft Docs  
* Peoplefone SIP Trunk Documentation

---

## **Step 3: ACS Call Handling and App Logic**

In your azure-ai-caller-main app:

* Endpoint for call events:

/acs/incoming in your FastAPI receives ACS events.

* Number filtering:

Enhance your call logic to validate the caller is Swiss (\+41... prefix) and only allow such calls to proceed—ignore or reject others.Example logic (in your inbound call handler):

*if* not caller\_id.startswith("+41"):

    logger.info(f"Rejected non-Swiss call from {caller\_id}")

    *return* PlainTextResponse("Only Swiss calls are processed", *status\_code*\=403)

* Peoplefone will send only Swiss numbers if configured properly, but this is a double layer of control.

---

## **Step 4: Azure Resource Wiring**

You have these Azure resources:

* acs-Telephone-Agent: Communication Service (calls, voice, PSTN integration)  
* database-telephone-agent: CosmosDB (logs, transcriptions, sessions, etc)  
* storageta: Blob Storage (files, recordings, documents)  
* search-telephone-agent: Azure AI Search (semantic/document search)  
* inboundcall-topic: Event Grid (notifications/event-driven processing, e.g., new call events)

Make sure:

* All deployments use the correct endpoints/keys (set via environment variables or Azure Key Vault).  
* Blob container, CosmosDB collections, and Search Index are set up with sufficient permissions for your app’s managed identity or service principal.

---

## **Step 5: Cloud and Network Security**

* Public-facing endpoints (for ACS event callbacks, document upload): Should be protected—use Azure API Management, Auth, or at least strong IP/firewall rules.  
* SBC and SIP trunk: Secure using TLS/SRTP where possible. Ensure only expected IPs/ports are allowed.  
* Move secrets out of code to Azure Key Vault—update your app to load from there.

---

## **Step 6: Testing and Go-Live**

1. Once Ribbon SBC is licensed/configured:  
* Initiate a test call from Swiss mobile/landline (via Peoplefone) to your assigned Swiss DID.  
* Ensure call flows through Peoplefone → Ribbon SBC → Azure ACS → Your FastAPI Agent.  
* Check inbound call appears and is processed in Azure (monitor logs in CosmosDB, etc).  
* Test from non-Swiss number to confirm filter works.  
1. Monitor/scale: Use Azure Monitor, Application Insights for production readiness.

---

## **Summary Diagram**

Swiss Number (--Peoplefone SIP Trunk--)

                |

                v

           Ribbon SBC (licensed, public IP)

                |

                v

      Azure Comm Service (acs-Telephone-Agent)

                |

                v

          FastAPI Bot (Azure VM/App Service/Container Instance)

                |

           \+----------+----------+

           |          |          |

      CosmosDB   BlobStorage   AI Search

---

## **References for Setup**

* ACS Direct Routing with SBC \- Microsoft Docs  
* Ribbon SBC Quick Start Guide  
* Peoplefone SIP Trunk Guide  
* Azure resource connection strings (set in your project's .env and config.py).

---

If you want a tailored checklist for infrastructure, step-by-step configuration scenarios, or sample configuration files, just ask\!  
I dont have any VM there on Azure where will I get this?

Great question\! Here’s a clear explanation:

---

## **What is the Ribbon SBC “HW ID” and Where Do You Get It?**

* The HW ID (Hardware ID) is a unique identifier that Ribbon uses to generate a license for your SBC (Session Border Controller).  
* This identifier is NOT tied to your application code or your Azure resources like Communication Services or CosmosDB etc.  
* The HW ID will be provided by the Ribbon SBC software (or virtual appliance) itself, not by your current Azure deployment.

---

## **Where Does the Ribbon SBC Run?**

* The SBC must run somewhere — either:  
* As a virtual machine hosted in Azure (most common for Azure-only deployments),  
* Or on physical hardware (on-premises, less common in cloud-native projects).

Since you currently do not have a VM deployed, you’ll need to create one to host the Ribbon SBC:

---

## **How to Get Your Ribbon SBC VM Set Up in Azure**

1\. Deploy a VM in Azure

* Go to Azure Portal → Virtual Machines → Create a new VM.  
* Choose a Windows or Linux base image, as suitable for your Ribbon SBC software (check Ribbon’s requirements).  
* Assign a static public IP (needed for SIP trunk and ACS).

2\. Install Ribbon SBC

* Download the Ribbon virtual SBC image from Ribbon support/website or Nuvola (they will guide you).  
* Deploy/install the SBC software onto your Azure VM following Ribbon’s documentation.

3\. Start the SBC Software

* The Ribbon admin interface will show you the HW ID (often in the system/license/status page of the web admin).

4\. Submit HW ID for Licensing

* Provide this HW ID to Nuvola/Ribbon via email so they can issue your temporary license.

---

## **Summary Table**

| Component | Hosting Location | Your Next Step |
| :---- | :---- | :---- |
| Ribbon SBC | Azure Virtual Machine | Create VM, install SBC |
| HW ID | SBC Admin Interface | Read from admin screen |
| License Activation | After HW ID provided | Upload license to SBC |
| SIP Routing | Ribbon SBC in Azure | Configure per docs |

---

## **References**

* Ribbon EdgeMarc and SBCs in Azure: Deployment Guide  
* Azure VM Quick Start

---

Summary:You must create an Azure VM, install Ribbon SBC on it, retrieve the HW ID from the admin portal, and send it to Nuvola/Ribbon for the license. Once licensed, you can proceed with SIP trunk and Azure Direct Routing setup.If you need more step-by-step guidance for creating the VM or installing the SBC, let me know your preferred OS (Windows/Linux) and I can provide detailed instructions.

# Great question

Great question\! Here’s a clear explanation:

---

## **What is the Ribbon SBC “HW ID” and Where Do You Get It?**

* The HW ID (Hardware ID) is a unique identifier that Ribbon uses to generate a license for your SBC (Session Border Controller).  
* This identifier is NOT tied to your application code or your Azure resources like Communication Services or CosmosDB etc.  
* The HW ID will be provided by the Ribbon SBC software (or virtual appliance) itself, not by your current Azure deployment.

---

## **Where Does the Ribbon SBC Run?**

* The SBC must run somewhere — either:  
* As a virtual machine hosted in Azure (most common for Azure-only deployments),  
* Or on physical hardware (on-premises, less common in cloud-native projects).

Since you currently do not have a VM deployed, you’ll need to create one to host the Ribbon SBC:

---

## **How to Get Your Ribbon SBC VM Set Up in Azure**

1\. Deploy a VM in Azure

* Go to Azure Portal → Virtual Machines → Create a new VM.  
* Choose a Windows or Linux base image, as suitable for your Ribbon SBC software (check Ribbon’s requirements).  
* Assign a static public IP (needed for SIP trunk and ACS).

2\. Install Ribbon SBC

* Download the Ribbon virtual SBC image from Ribbon support/website or Nuvola (they will guide you).  
* Deploy/install the SBC software onto your Azure VM following Ribbon’s documentation.

3\. Start the SBC Software

* The Ribbon admin interface will show you the HW ID (often in the system/license/status page of the web admin).

4\. Submit HW ID for Licensing

* Provide this HW ID to Nuvola/Ribbon via email so they can issue your temporary license.

---

## **Summary Table**

| Component | Hosting Location | Your Next Step |
| :---- | :---- | :---- |
| Ribbon SBC | Azure Virtual Machine | Create VM, install SBC |
| HW ID | SBC Admin Interface | Read from admin screen |
| License Activation | After HW ID provided | Upload license to SBC |
| SIP Routing | Ribbon SBC in Azure | Configure per docs |

---

## **References**

* Ribbon EdgeMarc and SBCs in Azure: Deployment Guide  
* Azure VM Quick Start

---

Summary:You must create an Azure VM, install Ribbon SBC on it, retrieve the HW ID from the admin portal, and send it to Nuvola/Ribbon for the license. Once licensed, you can proceed with SIP trunk and Azure Direct Routing setup.If you need more step-by-step guidance for creating the VM or installing the SBC, let me know your preferred OS (Windows/Linux) and I can provide detailed instructions.  
Should I directly create it or should I just ask Nuvola to get the requirements

The best practice is to ask Nuvola (or your Ribbon SBC vendor/support) for the exact requirements first before creating your VM. Here’s why:

---

## **Why Ask for Requirements First?**

1. Correct Sizing:

Ribbon SBCs can have specific requirements around CPU, RAM, storage, and network adapters depending on expected load (call volume, codecs, etc.). If you self-provision now, you might under-size or over-spend.

1. Supported OS/Platform:

Ribbon SBC virtual appliances may require a specific OS (Windows or Linux), sometimes even a specific distribution or VM image format (e.g., VHD, OVA).

1. Networking Setup:

You might need certain ports, firewall rules, static/public IPs, or even multiple NICs (network interfaces) for SBC “inside/outside leg.”

1. Feature Licensing:

Sometimes the VM size/license tier must match your planned Ribbon license/capacity (like FQDNs, transcoding, Direct Routing concurrency).

1. Nuvola’s Guidance:

Vendors sometimes have quickstart guides, scripts, or “golden images” for Azure deployments — they’ll save you time and possible misconfiguration.

---

## **Sample Message to Nuvola**

\> Hi Nuvola Team,\> Before I deploy the Ribbon SBC in Azure, could you please provide:\> \- The recommended (and minimum) VM sizing (CPU/RAM/disk)?\> \- The supported/required OS or VM image for the SBC?\> \- Any special network/subnet or firewall requirements?\> \- Any other prerequisites (e.g., Azure Marketplace listing, BYOL template, etc)?\>\> This will help me deploy the VM with the correct configuration for the license.

## **Summary Table**

| Option | Pros | Cons |
| :---- | :---- | :---- |
| Ask Nuvola first | Peace of mind, correct specs, vendor-supported, saves time if they have special process | Small delay, but best |
| Make VM now (guess spec) | Slightly faster, but risk of errors/waste | Risk of under/oversizing; rework |

---

Conclusion:You should ask Nuvola for the requirements before creating your VM. This ensures your environment is supported, right-sized, and ready for a smooth deployment and licensing process.If you’d like a template email or checklist, just ask\!  
