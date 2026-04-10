"""
Email service for sending call transcripts and notifications.
Uses Azure Communication Services Email or SendGrid for email delivery.
"""

import os
import logging
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails with call transcripts."""
    
    def __init__(self):
        self.sender_email = os.getenv("EMAIL_SENDER_ADDRESS", "donotreply@azurecomm.net")
        self.default_recipient = os.getenv("EMAIL_DEFAULT_RECIPIENT", "medcentervolta@hin.ch")
        self.connection_string = os.getenv("ACS_EMAIL_CONNECTION_STRING", "")
        self.sendgrid_api_key = os.getenv("SENDGRID_API_KEY", "")
        
        # Doctor email mapping (can be set via environment variable DOCTOR_EMAILS_JSON)
        # Format: {"mallisho": "mallisho@example.com", "lumpp": "lumpp@example.com", ...}
        import json
        doctor_emails_json = os.getenv("DOCTOR_EMAILS_JSON", "{}")
        try:
            self.doctor_emails = json.loads(doctor_emails_json)
        except:
            self.doctor_emails = {}
        
    def get_doctor_email(self, doctor_key: str) -> Optional[str]:
        """
        Get email address for a specific doctor.
        
        Args:
            doctor_key: Doctor key (e.g., 'mallisho', 'lumpp', 'keser', 'osterwalder')
            
        Returns:
            Email address or None if not configured
        """
        return self.doctor_emails.get(doctor_key.lower())
    
    def determine_recipient_from_transcript(self, transcript_data: List[Dict[str, Any]]) -> Optional[str]:
        """
        Determine the appropriate recipient based on transcript content.
        
        Args:
            transcript_data: List of transcription entries
            
        Returns:
            Email address or None to use default
        """
        if not transcript_data:
            return None
        
        # Combine all text from the transcript
        all_text = " ".join([
            entry.get("utterance_text", "").lower() 
            for entry in transcript_data 
            if entry.get("utterance_text")
        ])
        
        # Check for prescription-related keywords
        prescription_keywords = [
            "rezept", "prescription", "medikament", "medication", "verschreibung",
            "tabletten", "medizin", "arznei", "verschreiben"
        ]
        is_prescription = any(kw in all_text for kw in prescription_keywords)
        
        # Check for certificate/sick note keywords
        certificate_keywords = [
            "attest", "krankmeldung", "zeugnis", "certificate", "sick note",
            "arbeitsunfähig", "krank", "sick leave"
        ]
        is_certificate = any(kw in all_text for kw in certificate_keywords)
        
        # If prescription or certificate, try to find doctor
        if is_prescription or is_certificate:
            # Map of doctor names to their keys
            doctor_mappings = {
                "mallisho": "mallisho",
                "lumpp": "lumpp", 
                "keser": "keser",
                "osterwalder": "osterwalder"
            }
            
            # Check which doctor was mentioned
            for name, key in doctor_mappings.items():
                if name in all_text:
                    doctor_email = self.get_doctor_email(key)
                    if doctor_email:
                        logger.info(f"[EMAIL ROUTING] Routing to Dr. {key} ({doctor_email}) based on transcript")
                        return doctor_email
        
        return None
        
    async def send_transcript_email(
        self, 
        session_id: str,
        transcript_data: List[Dict[str, Any]],
        caller_info: Optional[Dict[str, str]] = None,
        recipient: Optional[str] = None
    ) -> bool:
        """
        Send call transcript via email.
        
        Args:
            session_id: Call session ID
            transcript_data: List of transcription entries with speaker and text
            caller_info: Optional caller details (phone number, etc.)
            recipient: Email recipient (defaults to medcentervolta@hin.ch)
            
        Returns:
            bool: True if email sent successfully
        """
        try:
            recipient = recipient or self.default_recipient
            
            # Format the transcript
            transcript_html = self._format_transcript_html(transcript_data)
            transcript_text = self._format_transcript_text(transcript_data)
            
            # Build email subject
            caller_phone = caller_info.get("phone", "Unknown") if caller_info else "Unknown"
            insurance_card_number = caller_info.get("insurance_card_number") if caller_info else None
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            subject = f"Call Transcript - {caller_phone} - {timestamp}"

            # Build insurance card HTML/text snippets (only for new patients)
            insurance_html = ""
            insurance_text = ""
            if insurance_card_number:
                insurance_html = f"<strong>Health Insurance Card No.:</strong> {insurance_card_number}<br>"
                insurance_text = f"Health Insurance Card No.: {insurance_card_number}"

            # Build email body
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <h2 style="color: #2c5aa0;">MedCenter Volta - Call Transcript</h2>

                <div style="background-color: #f4f4f4; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                    <strong>Session ID:</strong> {session_id}<br>
                    <strong>Caller:</strong> {caller_phone}<br>
                    <strong>Date:</strong> {timestamp}<br>
                    {insurance_html}
                </div>
                
                <h3 style="color: #2c5aa0;">Conversation Transcript:</h3>
                <div style="border-left: 3px solid #2c5aa0; padding-left: 15px;">
                    {transcript_html}
                </div>
                
                <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
                <p style="font-size: 12px; color: #666;">
                    This is an automated transcript from the MedCenter Volta AI Reception System.<br>
                    Please review for accuracy and follow up as needed.
                </p>
            </body>
            </html>
            """
            
            text_body = f"""MedCenter Volta - Call Transcript

Session ID: {session_id}
Caller: {caller_phone}
Date: {timestamp}
{insurance_text}

--- Conversation Transcript ---

{transcript_text}

---
This is an automated transcript from the MedCenter Volta AI Reception System.
Please review for accuracy and follow up as needed.
"""
            
            # Try Azure Communication Services Email first
            if self.connection_string:
                success = await self._send_via_acs_email(
                    recipient=recipient,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body
                )
                if success:
                    return True
            
            # Fallback to SendGrid
            if self.sendgrid_api_key:
                success = await self._send_via_sendgrid(
                    recipient=recipient,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body
                )
                if success:
                    return True
            
            logger.error("No email service configured. Set ACS_EMAIL_CONNECTION_STRING or SENDGRID_API_KEY.")
            return False
            
        except Exception as e:
            logger.error(f"Error sending transcript email: {e}")
            return False
    
    def _format_transcript_html(self, transcript_data: List[Dict[str, Any]]) -> str:
        """Format transcript as HTML."""
        if not transcript_data:
            return "<p><em>No transcript available</em></p>"
        
        html_parts = []
        for entry in transcript_data:
            speaker = entry.get("speaker", "unknown")
            text = entry.get("utterance_text", "")
            timestamp = entry.get("timestamp", "")
            
            # Format timestamp
            time_str = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M:%S")
                except:
                    pass
            
            # Speaker styling
            if speaker == "customer" or speaker == "caller":
                bg_color = "#e3f2fd"
                label = "Caller"
                align = "left"
            else:
                bg_color = "#f3e5f5"
                label = "AI Assistant"
                align = "right"
            
            html_parts.append(f"""
                <div style="margin: 10px 0; padding: 10px; background-color: {bg_color}; border-radius: 5px; text-align: {align};">
                    <strong>{label}</strong> <span style="font-size: 11px; color: #666;">{time_str}</span><br>
                    <span>{text}</span>
                </div>
            """)
        
        return "\n".join(html_parts)
    
    def _format_transcript_text(self, transcript_data: List[Dict[str, Any]]) -> str:
        """Format transcript as plain text."""
        if not transcript_data:
            return "No transcript available"
        
        lines = []
        for entry in transcript_data:
            speaker = entry.get("speaker", "unknown")
            text = entry.get("utterance_text", "")
            timestamp = entry.get("timestamp", "")
            
            time_str = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M:%S")
                except:
                    pass
            
            label = "Caller" if speaker in ["customer", "caller"] else "AI"
            lines.append(f"[{time_str}] {label}: {text}")
        
        return "\n".join(lines)
    
    async def _send_via_acs_email(
        self, 
        recipient: str, 
        subject: str, 
        html_body: str, 
        text_body: str
    ) -> bool:
        """Send email via Azure Communication Services Email."""
        try:
            from azure.communication.email import EmailClient
            
            client = EmailClient.from_connection_string(self.connection_string)
            
            message = {
                "senderAddress": self.sender_email,
                "recipients": {
                    "to": [{"address": recipient}]
                },
                "content": {
                    "subject": subject,
                    "plainText": text_body,
                    "html": html_body
                }
            }
            
            poller = client.begin_send(message)
            result = await asyncio.to_thread(poller.result)

            logger.info(f"Email sent successfully via ACS to {recipient}")
            return True
            
        except Exception as e:
            logger.warning(f"ACS email failed: {e}")
            return False
    
    async def _send_via_sendgrid(
        self, 
        recipient: str, 
        subject: str, 
        html_body: str, 
        text_body: str
    ) -> bool:
        """Send email via SendGrid as fallback."""
        try:
            url = "https://api.sendgrid.com/v3/mail/send"
            
            payload = {
                "personalizations": [{
                    "to": [{"email": recipient}]
                }],
                "from": {"email": self.sender_email},
                "subject": subject,
                "content": [
                    {"type": "text/plain", "value": text_body},
                    {"type": "text/html", "value": html_body}
                ]
            }
            
            headers = {
                "Authorization": f"Bearer {self.sendgrid_api_key}",
                "Content-Type": "application/json"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status in [200, 202]:
                        logger.info(f"Email sent successfully via SendGrid to {recipient}")
                        return True
                    else:
                        logger.warning(f"SendGrid failed with status {response.status}")
                        return False
                        
        except Exception as e:
            logger.warning(f"SendGrid email failed: {e}")
            return False


# Singleton instance
email_service = EmailService()
