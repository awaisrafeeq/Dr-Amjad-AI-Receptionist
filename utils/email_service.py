"""
Email service for sending call transcripts and notifications.
Uses Azure Communication Services Email or SendGrid for email delivery.
"""

import os
import logging
import asyncio
import html
import re
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

    def _parse_recipients(self, recipient: Optional[str]) -> List[str]:
        """Parse one or more comma/semicolon-separated email recipients."""
        raw = recipient or self.default_recipient
        recipients: List[str] = []
        seen = set()
        for email in re.split(r"[,;]", raw):
            email = email.strip()
            if not email:
                continue
            email_key = email.lower()
            if email_key in seen:
                continue
            seen.add(email_key)
            recipients.append(email)
        return recipients
    
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
            recipients = self._parse_recipients(recipient)
            if not recipients:
                logger.error("No email recipients configured. Set EMAIL_DEFAULT_RECIPIENT.")
                return False
            
            # Format the transcript
            transcript_html = self._format_transcript_html(transcript_data)
            transcript_text = self._format_transcript_text(transcript_data)
            summary_text = await self._build_german_summary(transcript_data, transcript_text)
            summary_html = self._format_summary_html(summary_text)
            
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

                <h3 style="color: #2c5aa0; margin-top: 25px;">Kurze Zusammenfassung (Deutsch):</h3>
                {summary_html}
                
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

--- Kurze Zusammenfassung (Deutsch) ---

{summary_text}

---
This is an automated transcript from the MedCenter Volta AI Reception System.
Please review for accuracy and follow up as needed.
"""
            
            # Try Azure Communication Services Email first
            if self.connection_string:
                success = await self._send_via_acs_email(
                    recipients=recipients,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body
                )
                if success:
                    return True
            
            # Fallback to SendGrid
            if self.sendgrid_api_key:
                success = await self._send_via_sendgrid(
                    recipients=recipients,
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

    async def _build_german_summary(self, transcript_data: List[Dict[str, Any]], transcript_text: str) -> str:
        """Create a medium-length German paragraph summary for the transcript email."""
        llm_summary = await self._generate_german_summary_with_llm(transcript_text)
        if llm_summary:
            return llm_summary
        return self._build_fallback_german_summary(transcript_data)

    async def _generate_german_summary_with_llm(self, transcript_text: str) -> Optional[str]:
        """Use Azure OpenAI to summarize the full transcript, with safe fallback on any error."""
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_KEY")
        api_version = os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION") or os.getenv("AZURE_OPENAI_API_VERSION")
        deployment = (
            os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        )

        if not endpoint or not api_key or not api_version or not deployment:
            logger.warning("[EMAIL SUMMARY] Azure OpenAI summary config missing; using fallback summary")
            return None

        safe_transcript = (transcript_text or "").strip()
        if not safe_transcript:
            return None
        if len(safe_transcript) > 14000:
            safe_transcript = safe_transcript[-14000:]

        try:
            from openai import AsyncAzureOpenAI

            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key,
                api_version=api_version,
            )
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=deployment,
                    temperature=0.2,
                    max_tokens=220,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Du fasst Telefontranskripte fuer eine Arztpraxis zusammen. "
                                "Schreibe ausschliesslich auf Deutsch, als einen einzigen mittellangen Absatz. "
                                "Keine Bulletpoints, keine Ueberschrift. "
                                "Laenge: 4 bis 5 Saetze, etwa 70 bis 110 Woerter. "
                                "Erwaehne nur wichtige Punkte: Anliegen, Patientendaten/Identifikation falls relevant, Arzt, Terminwunsch, finaler gebuchter Termin oder Ergebnis. "
                                "Schreibe Patientennamen immer in lateinischen Buchstaben, auch wenn sie im Transkript in arabischer, chinesischer oder anderer Schrift vorkommen. "
                                "Wenn der Anrufer sich korrigiert hat, verwende nur die final bestaetigte Information. "
                                "Keine erfundenen Details."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Transkript:\n{safe_transcript}",
                        },
                    ],
                ),
                timeout=12,
            )
            summary = (response.choices[0].message.content or "").strip()
            summary = re.sub(r"\s+", " ", summary)
            if summary:
                logger.info("[EMAIL SUMMARY] Generated German summary via Azure OpenAI")
                return summary
        except Exception as e:
            logger.warning(f"[EMAIL SUMMARY] Azure OpenAI summary failed; using fallback summary: {e}")
        return None

    def _build_fallback_german_summary(self, transcript_data: List[Dict[str, Any]]) -> str:
        """Fallback summary when LLM generation is unavailable."""
        if not transcript_data:
            return "Es liegt keine Transkription vor."

        cleaned_entries = []
        for entry in transcript_data:
            speaker = entry.get("speaker", "unknown")
            text = self._clean_summary_text(entry.get("utterance_text", ""))
            if text:
                cleaned_entries.append({"speaker": speaker, "text": text})

        if not cleaned_entries:
            return "Es wurden keine wichtigen Punkte automatisch erkannt."

        all_text = " ".join(item["text"].lower() for item in cleaned_entries)
        summary: List[str] = []

        if any(word in all_text for word in ("termin", "appointment", "book", "scheduled", "gebucht")):
            summary.append("Es ging hauptsaechlich um eine Terminvereinbarung")
        elif any(word in all_text for word in ("rezept", "prescription", "medikament", "medication")):
            summary.append("Es ging hauptsaechlich um eine Rezept- oder Medikamentenanfrage")
        elif any(word in all_text for word in ("zeugnis", "attest", "certificate", "sick note", "krankmeldung")):
            summary.append("Es ging hauptsaechlich um ein aerztliches Zeugnis oder eine Krankmeldung")

        doctor_name = self._extract_doctor_name(" ".join(item["text"] for item in cleaned_entries))
        if doctor_name:
            summary.append(f"Der Arztbezug war {doctor_name}")

        outcome_line = self._find_last_matching_line(
            cleaned_entries,
            ("scheduled", "booked", "appointment has been", "termin ist", "termin wurde", "gebucht", "vereinbart", "buchen"),
        )
        if outcome_line:
            appointment_detail = self._extract_appointment_detail(outcome_line)
            if appointment_detail:
                summary.append(f"Als Ergebnis wurde ein Termin vereinbart oder bestaetigt ({appointment_detail})")
            else:
                summary.append("Als Ergebnis wurde der Termin im Gespraech vereinbart oder weiter bearbeitet")

        important_customer_line = self._find_first_matching_line(
            [item for item in cleaned_entries if item["speaker"] in ("customer", "caller")],
            ("ich brauche", "i need", "i want", "moechte", "möchte", "rezept", "termin", "appointment", "prescription"),
        )
        if important_customer_line and not summary:
            summary.append(self._describe_customer_need(important_customer_line).rstrip("."))

        if not summary:
            fallback = next((item["text"] for item in cleaned_entries if item["speaker"] in ("customer", "caller")), "")
            if fallback:
                summary.append(f"Eine wichtige Angabe des Anrufers war: {fallback}")
            else:
                summary.append("Es wurden keine wichtigen Punkte automatisch erkannt")

        paragraph = ". ".join(part.strip().rstrip(".") for part in summary[:4] if part.strip())
        return paragraph[:1].upper() + paragraph[1:] + "."

    def _clean_summary_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if not text:
            return ""
        if len(text.split()) <= 2 and text.lower().strip(".!?") in {"yes", "yeah", "ok", "okay", "no", "bye", "ja", "nein"}:
            return ""
        if len(text) > 220:
            text = text[:217].rstrip() + "..."
        return text

    def _find_first_matching_line(self, entries: List[Dict[str, str]], keywords: tuple) -> str:
        for item in entries:
            text = item["text"]
            lower = text.lower()
            if any(keyword in lower for keyword in keywords):
                return text
        return ""

    def _find_last_matching_line(self, entries: List[Dict[str, str]], keywords: tuple) -> str:
        for item in reversed(entries):
            text = item["text"]
            lower = text.lower()
            if any(keyword in lower for keyword in keywords):
                return text
        return ""

    def _extract_doctor_name(self, text: str) -> str:
        match = re.search(r"\bDr\.?\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß'-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß'-]+)?", text)
        if match:
            return match.group(0).strip()
        return ""

    def _extract_appointment_detail(self, text: str) -> str:
        details = []
        date_match = re.search(
            r"\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}-\d{2}-\d{2}|"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Januar|Februar|Maerz|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|"
            r"\d{1,2}\.\s*(?:Januar|Februar|Maerz|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember))\b",
            text,
            re.IGNORECASE,
        )
        time_match = re.search(r"\b\d{1,2}[:.]\d{2}\b", text)
        if date_match:
            details.append(date_match.group(0).strip())
        if time_match:
            details.append(time_match.group(0).replace(".", ":"))
        return ", ".join(details)

    def _describe_customer_need(self, text: str) -> str:
        lower = text.lower()
        if any(word in lower for word in ("appointment", "termin", "book")):
            return "Wichtige Angabe des Anrufers: Der Anrufer wollte einen Termin vereinbaren."
        if any(word in lower for word in ("prescription", "rezept", "medication", "medikament")):
            return "Wichtige Angabe des Anrufers: Der Anrufer hatte eine Rezept- oder Medikamentenanfrage."
        return "Wichtige Angabe des Anrufers: Der Anrufer hatte ein allgemeines Anliegen."

    def _format_summary_html(self, summary_text: str) -> str:
        return f"""
                <div style="background-color: #eef6ff; border-left: 3px solid #2c5aa0; padding: 12px 15px; border-radius: 5px;">
                    <p style="margin: 0;">{html.escape(summary_text)}</p>
                </div>
        """
    
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
        recipients: List[str], 
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
                    "to": [{"address": email} for email in recipients]
                },
                "content": {
                    "subject": subject,
                    "plainText": text_body,
                    "html": html_body
                }
            }
            
            poller = client.begin_send(message)
            result = await asyncio.to_thread(poller.result)

            logger.info(f"Email sent successfully via ACS to {', '.join(recipients)}")
            return True
            
        except Exception as e:
            logger.warning(f"ACS email failed: {e}")
            return False
    
    async def _send_via_sendgrid(
        self, 
        recipients: List[str], 
        subject: str, 
        html_body: str, 
        text_body: str
    ) -> bool:
        """Send email via SendGrid as fallback."""
        try:
            url = "https://api.sendgrid.com/v3/mail/send"
            
            payload = {
                "personalizations": [{
                    "to": [{"email": email} for email in recipients]
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
                        logger.info(f"Email sent successfully via SendGrid to {', '.join(recipients)}")
                        return True
                    else:
                        logger.warning(f"SendGrid failed with status {response.status}")
                        return False
                        
        except Exception as e:
            logger.warning(f"SendGrid email failed: {e}")
            return False


# Singleton instance
email_service = EmailService()
