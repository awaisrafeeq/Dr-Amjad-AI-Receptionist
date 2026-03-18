`# Peoplefone to Azure AI Calling Agent Integration (Ribbon SBC & ACS)

## Prerequisites
- Azure VM running Ribbon SBC (Azure Marketplace image)
- Ribbon temporary license file (e.g., LIC_XXX.xml)
- Peoplefone SIP trunk credentials (Registrar, Username, Password)
- Azure Communication Services (ACS) resource
- FastAPI Agent accessible from public web
- Ribbon SBC admin credentials

---

## 1. Access Ribbon SBC Web Management
1. In Azure Portal, get **public IP** of SBC VM.
2. Open browser: `https://<SBC-Public-IP>`
3. Log in (default/set admin credentials).

---

## 2. Install the Ribbon SBC License
1. Navigate to **Administration > License Management** (exact name may vary)
2. Click **Upload/Install License**
3. Select your XML license file and upload
4. Confirm license active for 10 SIP sessions in **Licensing Status** or **System Info**
5. Reboot SBC if prompted

---

## 3. Configure Peoplefone SIP Trunk
1. Go to **Configuration > SIP Trunks** or **Peers/Service Providers**
2. Click **Add** or **Create New** SIP Trunk
3. Fill in:
   - Name: `Peoplefone`
   - Registrar/Proxy: `sips.peoplefone.cn`
   - Port: `5060` (use `5061` for TLS if required by Peoplefone)
   - Username: `90724300504`
   - Password: (your actual password)
   - Authentication: Enabled
   - Transport: `UDP` or `TLS` (confirm with Peoplefone)
   - Domain: `peoplefone.cn` (if required)
   - Registration: Enabled
4. Save/apply
5. Verify trunk shows **Registered** in Trunk status page

---

## 4. Route Peoplefone Calls to ACS
1. Go to **Call Routing** or **Dial Plan**
2. Create rule:
   - Match Condition: All inbound DID/calls from Peoplefone
   - Action: Forward to ACS SIP peer (define next step)

---

## 5. Configure SIP Peer to ACS
1. Go to **SIP Trunks/Peers**, click **Add**
2. Fill in:
   - Name: `AzureACS`
   - SIP Server/Domain: `sip.pstnhub.communication.azure.com` or `switzerlandnorth.pstnhub.communication.azure.com`
   - Port: `5061`
   - Transport: `TLS`
   - FQDN Routing: Enabled (if supported)
3. No credentials needed toward ACS
4. Save/apply
5. Make sure correct route so Peoplefone→Ribbon→ACS

---

## 6. Azure & SBC Firewall Rules
1. In Azure VM NSG, allow:
   - SIP: 5060/5061 TCP/UDP (as needed)
   - RTP: 10000–20000 UDP
   - Web Admin: 443/80 TCP (restrict source IP)
2. Confirm ports open on VM and SBC as needed

---

## 7. Configure ACS for Direct Routing
1. In Azure Portal, ACS resource settings:
   - Add/allow SBC VM public IP as trusted
   - Register required trunks/domains
2. See ACS docs: https://learn.microsoft.com/en-us/azure/communication-services/concepts/voice-video-calling/direct-routing-sbc

---

## 8. ACS Event Callback URL
1. In ACS/Event Grid, set callback URL to:
   - `https://<your-app-endpoint>/acs/incoming`
2. FastAPI will process/answer inbound calls

---

## 9. Testing
1. Dial the Peoplefone number externally
2. Validate call hits SBC, routes to ACS, is answered by FastAPI
3. Watch logs for call events and troubleshooting

---

## Troubleshooting
- Check SIP trunk status for registration
- Verify SBC logs for call routing/failures
- Ensure firewall permits all signaling/media ports
- Confirm TLS/certs for encrypted signaling if used

---

## References
- [Peoplefone Support](https://www.peoplefone.com/ch-en/support)
- [Ribbon SBC Docs](https://support.ribboncommunications.com/)
- [Microsoft ACS Direct Routing](https://learn.microsoft.com/en-us/azure/communication-services/concepts/voice-video-calling/direct-routing-overview)

