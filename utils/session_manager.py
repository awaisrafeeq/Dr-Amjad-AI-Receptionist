"""
Session Manager for tracking call sessions and coordinating logging
"""

import logging
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from uuid import uuid4
from dataclasses import dataclass, asdict, field
from difflib import SequenceMatcher
from utils.azure_storage_logger import storage_logger
from utils.phonebook_lookup import get_phonebook_lookup

logger = logging.getLogger(__name__)


def _phonebook_for_model(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    return {key: (value if value not in (None, "") else "MISSING") for key, value in data.items()}


def _normalize_for_match(value: Optional[str]) -> str:
    if not value:
        return ""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(normalized.strip().lower().split())


def _name_similarity(left: Optional[str], right: Optional[str]) -> float:
    left_norm = _normalize_for_match(left)
    right_norm = _normalize_for_match(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()

@dataclass
class CallSession:
    """Data class representing a call session."""
    session_id: str
    history_id: str
    start_time: datetime
    participants: List[Dict[str, Any]]  
    direction: str = "inbound"  # inbound 
    platform: str = "Azure Communication Services"
    end_time: Optional[datetime] = None
    status: str = "incoming"  # incoming, active, completed, failed, disconnected
    intent_summary: Optional[str] = None
    total_duration: Optional[float] = 0.0
    call_connection_id: Optional[str] = None
    server_call_id: Optional[str] = None
    correlation_id: Optional[str] = None
    caller_id: Optional[str] = None
    transcription_count: int = 0
    event_count: int = 0
    phonebook_match: Optional[Dict[str, Any]] = None
    phonebook_candidates: List[Dict[str, Any]] = field(default_factory=list)
    office_handoff_sent: bool = False
    office_handoff_in_progress: bool = False
    office_handoff_pending: Optional[Dict[str, Any]] = None
    appointment_booked: bool = False
    recent_transcript: List[Dict[str, Any]] = field(default_factory=list)
    safety_review_in_progress: bool = False
    last_safety_review_at: float = 0.0

class SessionManager:
    """Manages call sessions and coordinates logging activities."""
    
    def __init__(self):
        self.active_sessions: Dict[str, CallSession] = {}
        self._session_lock = asyncio.Lock()
        self._cleanup_in_progress: set = set()  # guards against concurrent cleanup from WS + CallDisconnected
        self._hangup_in_progress: set[str] = set()
        self._hangup_completed: set[str] = set()
        self._realtime_sessions: set[str] = set()
        # self.session_call_mapping: Dict[str, str] = {}  # call_connection_id -> session_id

    def _hangup_key(self, session_id: str, call_connection_id: str) -> str:
        return f"{session_id}:{call_connection_id}"

    async def begin_hangup(self, session_id: str, call_connection_id: str) -> bool:
        """Reserve a hangup attempt for this session/call pair.

        Returns False when another realtime loop already sent or is sending the
        same hangup. This must be shared across RTMiddleTier instances because
        ACS can create overlapping websocket loops for one call.
        """
        key = self._hangup_key(session_id, call_connection_id)
        async with self._session_lock:
            if key in self._hangup_completed or key in self._hangup_in_progress:
                return False
            self._hangup_in_progress.add(key)
            return True

    async def finish_hangup(self, session_id: str, call_connection_id: str, success: bool) -> None:
        key = self._hangup_key(session_id, call_connection_id)
        async with self._session_lock:
            self._hangup_in_progress.discard(key)
            if success:
                self._hangup_completed.add(key)

    async def begin_cleanup(self, session_id: Optional[str]) -> bool:
        """Reserve session cleanup so WS and ACS callbacks cannot both run it."""
        if not session_id:
            return False
        async with self._session_lock:
            if session_id in self._cleanup_in_progress:
                return False
            self._cleanup_in_progress.add(session_id)
            return True

    async def begin_realtime_session(self, session_id: Optional[str]) -> bool:
        """Allow only one active OpenAI realtime bridge per call session."""
        if not session_id:
            return True
        async with self._session_lock:
            if session_id in self._realtime_sessions:
                return False
            self._realtime_sessions.add(session_id)
            return True

    async def end_realtime_session(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        async with self._session_lock:
            self._realtime_sessions.discard(session_id)
        
    async def create_session(self, event: Dict[Any, Any], event_type: str) -> str:
        """
        Create a new call session.
        
        Args:
            event: Incoming call event data
        Returns:
            str: Session ID
        """
        session_id = str(uuid4())
        start_time = datetime.now(timezone.utc)
        direction = "inbound"  
        status = "incoming"
        platform = "Azure Communication Services"
        
        
        # User details
        role_caller = "customer"
        phone_number_caller = event["from"]["phoneNumber"]["value"]
        kind_caller = event["from"]["kind"]

        phonebook_candidates: List[Dict[str, Any]] = []
        try:
            lookup = get_phonebook_lookup()
            if lookup is not None:
                phonebook_candidates = [
                    match.to_dict() for match in lookup.lookup_candidates_by_phone(phone_number_caller)
                ]
        except Exception as e:
            logger.warning("Phonebook lookup failed (internal only): %s", e)
        
        # calling agent details
        role_agent = "agent"
        phone_number_agent = event["to"]["phoneNumber"]["value"]
        kind_agent = event["to"]["kind"]
        
        
        # Create session object
        session = CallSession(
            session_id=session_id,
            start_time=start_time,
            history_id=str(uuid4()),
            participants=[
                {
                    'role': role_caller,
                    'phone_number': phone_number_caller,
                    'kind': kind_caller
                },
                {
                    'role': role_agent,
                    'phone_number': phone_number_agent,
                    'kind': kind_agent
                }
            ],
            direction=direction,
            platform=platform,
            status=status,
            phonebook_candidates=phonebook_candidates,
        )

        # Store in active sessions
        async with self._session_lock:
            self.active_sessions[session_id] = session
        
        # Log initial call metadata
        metadata_doc = {
            'id': session_id, 
            'sessionId': session_id,
            'start_time': start_time.isoformat(),
            'direction': direction,
            'participants': [
                {
                    'role': role_caller,
                    'phone_number': phone_number_caller,
                    'kind': kind_caller
                },
                {
                    'role': role_agent,
                    'phone_number': phone_number_agent,
                    'kind': kind_agent
                }
            ],
            'platform': platform,
            'status': status,
            'internal_phonebook_candidate_count': len(phonebook_candidates),
        }
        
        phone_number = session.participants[0]['phone_number'] if session.participants else "unknown"
        history_doc = {
            "sessionId": session_id,
            "date": start_time.isoformat(),
            "direction": direction,
        }
        
        try:
            logger.info(f"[SESSION] Created: {session_id}")
            await storage_logger.log_call_metadata(metadata_doc)
            await self.log_event(session_id, {
                'event_type': event_type,
                'timestamp': start_time.isoformat(),
                'internal_phonebook_candidate_count': len(phonebook_candidates),
            })
            await storage_logger.log_call_history(session.history_id, phone_number, history_doc)
            
            return session_id
            
        except Exception as e:
            logger.error(f"[SESSION] Create error: {e}")
            # Clean up if logging failed
            async with self._session_lock:
                self.active_sessions.pop(session_id, None)
            raise
            
    async def end_session(self, session_id: Optional[str] = None, event_data: Optional[Dict[str, Any]] = None):
        """
        End a call session.
        
        Args:
            session_id: Session ID
            event_data: Event data containing end reason
            
        Returns:
            bool: Success status
        """
        try:
                
            async with self._session_lock:
                if not session_id or session_id not in self.active_sessions:
                    logger.warning(f"[SESSION] Not found: {session_id}")
                    self._cleanup_in_progress.discard(session_id)
                    return False
                session = self.active_sessions[session_id]
            
            if event_data is not None:
                end_time_str = event_data.get("timestamp", datetime.now(timezone.utc))
                session.end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
                session.status = event_data.get("status", session.status)
                
            
            # Calculate duration
            if session.start_time:
                if session.end_time and session.start_time:
                    session.total_duration = (session.end_time - session.start_time).total_seconds()
                else:
                    session.total_duration = None
                
            # Log session completion
            await self.log_event(session_id, event_data)
            
            # Log final metadata
            metadata_doc = {
                'id': session_id, 
                'sessionId': session_id,
                'start_time': session.start_time.isoformat(),
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'total_duration': session.total_duration,
                'CallConnectionId': session.call_connection_id,
                'ServerCallId': session.server_call_id,
                'CorrelationId': session.correlation_id,
                'direction': session.direction,
                'participants': session.participants,
                'total_transcriptions': session.transcription_count,
                'platform': session.platform,
                'status': session.status,
            }
            
            await storage_logger.log_call_metadata(metadata_doc)
            
            phone_number = session.participants[0]['phone_number'] if session.participants else "unknown"
            history_doc = {
                "PartitionKey": phone_number,
                "RowKey": session.history_id,
                "sessionId": session_id,
                "date": session.start_time.isoformat(),
                "call_connection_id": session.call_connection_id,
                "end_time": session.end_time.isoformat(),
                "direction": session.direction,
                "Duration": session.total_duration,
            }
            await storage_logger.log_call_history(session.history_id, phone_number, history_doc)
            
            # Remove from active sessions
            async with self._session_lock:
                self.active_sessions.pop(session_id, None)
                if session_id:
                    prefix = f"{session_id}:"
                    self._hangup_in_progress = {
                        key for key in self._hangup_in_progress if not key.startswith(prefix)
                    }
                    self._hangup_completed = {
                        key for key in self._hangup_completed if not key.startswith(prefix)
                    }
                    self._realtime_sessions.discard(session_id)
            self._cleanup_in_progress.discard(session_id)

            logger.info(f"[SESSION] Ended: {session_id}")
            return True

        except Exception as e:
            logger.error(f"[SESSION] End error: {e}")
            self._cleanup_in_progress.discard(session_id)
            return False
            
    async def log_event(self, session_id: Optional[str] = None, event_data: Optional[Dict[str, Any]] = None):
        """
        Log a session event.
        
        Args:
            session_id: Session ID
            event_data: Event data dictionary
        """
        try:
                
            if not session_id:
                logger.debug("No session for event")
                return
            
            log_id = str(uuid4())
            
            async with self._session_lock:
                session_data = self.active_sessions.get(session_id)
            
            if event_data is not None:
                
                if 'callConnectionId' in event_data:
                    call_connection_id = event_data['callConnectionId']
                    if session_data:
                        session_data.call_connection_id = call_connection_id
                    # self.session_call_mapping[call_connection_id] = session_id
                if 'serverCallId' in event_data:
                    server_call_id = event_data['serverCallId']
                    if session_data:
                        session_data.server_call_id = server_call_id
                if 'correlationId' in event_data:
                    correlation_id = event_data['correlationId']
                    if session_data:
                        session_data.correlation_id = correlation_id
                
                if session_data and event_data.get('status') != session_data.status:
                    if session_data:
                        session_data.status = event_data.get('status', session_data.status)
                    
                
                # 'participants': metadata_doc['participants'],
                # 'platform': platform,
                # 'status': status
                event_data['participants'] = session_data.participants if session_data else []
                event_data['platform'] = session_data.platform if session_data else "Unknown"
                event_data['status'] = session_data.status if session_data else "Unknown"
                
                await storage_logger.log_session_event(log_id, session_id, event_data)
            else:
                logger.debug(f"Empty event for {session_id}")
            
            # Update session stats
            if session_id in self.active_sessions:
                self.active_sessions[session_id].event_count += 1
                
            logger.debug(f"[EVENT] {session_id[:8]}: {event_data['event_type'] if event_data else 'Unknown'}")
            
        except Exception as e:
            logger.error(f"[EVENT] Log error: {e}")
    
    async def log_transcription(self, session_id: str, speaker: str, utterance_text: str, 
                               timestamp: Optional[str] = None) -> str:
        """
        Log transcription data for a session.
        
        Args:
            session_id: Session ID
            speaker: Speaker identifier (e.g., "customer", "agent")
            utterance_text: The transcribed text
            timestamp: Timestamp (ISO format)
            
        Returns:
            str: Transcription ID
        """
        try:
            # Create a more unique timestamp if one wasn't provided
            if timestamp:
                # Ensure timestamp is unique by adding microseconds if needed
                unique_timestamp = timestamp
            else:
                # Use high-precision timestamp with microseconds
                unique_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            
            # Create transcription ID based on session, speaker, timestamp and content hash
            # This ensures uniqueness even for rapid-fire transcriptions
            import hashlib
            content_hash = hashlib.md5(f"{session_id}_{speaker}_{unique_timestamp}_{utterance_text}".encode()).hexdigest()[:8]
            transcription_id = f"{session_id}_{speaker}_{content_hash}"
            
            transcription_data = {
                "speaker": speaker,
                "timestamp": unique_timestamp,
                "utterance_text": utterance_text
            }
            
            # Use sessionId for Cosmos DB partition key
            transcription_data["sessionId"] = session_id
            
            await storage_logger.log_transcription(transcription_id, session_id, transcription_data)
            
            # Update session stats
            if session_id in self.active_sessions:
                session = self.active_sessions[session_id]
                session.transcription_count += 1
                session.recent_transcript.append(
                    {
                        "speaker": speaker,
                        "utterance_text": utterance_text,
                        "timestamp": unique_timestamp,
                    }
                )
                session.recent_transcript = session.recent_transcript[-16:]
                if speaker == "customer":
                    self.maybe_start_safety_review(session_id)
            
            logger.debug(f"[TRANSCRIPT] {session_id[:8]}: {speaker} - {utterance_text[:30]}...")
            return transcription_id
            
        except Exception as e:
            logger.error(f"[TRANSCRIPT] Error: {e}")
            raise

    def maybe_start_safety_review(self, session_id: str) -> None:
        session = self.active_sessions.get(session_id)
        if (
            not session
            or session.office_handoff_sent
            or session.office_handoff_in_progress
            or session.office_handoff_pending
        ):
            return

        now = time.monotonic()
        if session.safety_review_in_progress or (now - session.last_safety_review_at) < 8:
            return

        customer_text = " ".join(
            entry.get("utterance_text", "")
            for entry in session.recent_transcript
            if entry.get("speaker") == "customer"
        ).strip()
        if len(customer_text) < 10:
            return
        if self._is_language_preference_only(customer_text):
            return

        session.safety_review_in_progress = True
        session.last_safety_review_at = now
        asyncio.create_task(self._run_safety_review(session_id))

    async def _run_safety_review(self, session_id: str) -> None:
        try:
            from utils.ai_safety_judge import ai_safety_judge

            session = self.active_sessions.get(session_id)
            if not session:
                return

            assessment = await ai_safety_judge.assess(session.recent_transcript)
            logger.info(
                "[SAFETY JUDGE] session=%s handoff=%s reason=%s urgency=%s confidence=%.2f",
                session_id[:8],
                assessment.should_handoff,
                assessment.reason,
                assessment.urgency,
                assessment.confidence,
            )

            if not assessment.should_handoff:
                return

            if not self._should_queue_safety_handoff(assessment):
                logger.info(
                    "[SAFETY JUDGE] Not queuing office handoff during live call for session=%s reason=%s urgency=%s",
                    session_id[:8],
                    assessment.reason,
                    assessment.urgency,
                )
                return

            if assessment.reason == "caller_requests_staff" and not self._recent_transcript_has_staff_request(session.recent_transcript):
                logger.info(
                    "[SAFETY JUDGE] Ignoring caller_requests_staff without an explicit staff request session=%s",
                    session_id[:8],
                )
                return

            summary = assessment.office_summary or "AI safety judge requested manual office review based on the live call transcript."
            await self.send_office_handoff_email(
                session_id=session_id,
                reason=assessment.reason,
                summary=summary,
                urgency=assessment.urgency,
            )
        except Exception as e:
            logger.error(f"[SAFETY JUDGE] Error for {session_id}: {e}")
        finally:
            session = self.active_sessions.get(session_id)
            if session:
                session.safety_review_in_progress = False

    def _should_queue_safety_handoff(self, assessment: Any) -> bool:
        """Decide which AI safety findings should become an office follow-up.

        Same-day routine symptoms are handled inside the call flow. We only
        queue office follow-up automatically for true emergency risk or when
        the conversation itself can no longer be handled safely by the agent.
        """
        if assessment.confidence < 0.75:
            return False
        if assessment.reason == "urgent_medical":
            return assessment.urgency == "emergency"
        return assessment.reason in {
            "confused_or_incoherent",
            "repeated_misunderstanding",
            "caller_requests_staff",
        }

    def _is_language_preference_only(self, customer_text: str) -> bool:
        text = _normalize_for_match(customer_text)
        if not text:
            return True
        language_tokens = {
            "speak english",
            "speaking english",
            "english",
            "english please",
            "in english",
            "speak in english",
            "deutsch",
            "german",
            "speak german",
            "auf deutsch",
            "francais",
            "french",
            "italian",
            "spanish",
            "turkish",
            "arabic",
            "kurdish",
        }
        compact = text.replace(".", "").strip()
        return compact in language_tokens

    def _recent_transcript_has_staff_request(self, transcript: List[Dict[str, Any]]) -> bool:
        text = " ".join(
            str(entry.get("utterance_text") or "")
            for entry in transcript[-12:]
            if entry.get("speaker") == "customer"
        ).lower()
        staff_phrases = (
            "human",
            "person",
            "staff",
            "reception",
            "receptionist",
            "office",
            "team",
            "callback",
            "call back",
            "call me",
            "transfer",
            "representative",
            "mitarbeiter",
            "praxis",
            "rueckruf",
            "rückruf",
            "zurueckrufen",
            "zurückrufen",
        )
        return any(phrase in text for phrase in staff_phrases)
    
    def get_session(self, session_id: str) -> Optional[CallSession]:
        """Get session by session ID."""
        return self.active_sessions.get(session_id)
        
    def get_active_sessions(self) -> List[CallSession]:
        """Get all active sessions."""
        return list(self.active_sessions.values())
        
    def get_session_by_call_connection_id(self, call_connection_id: str) -> Optional[CallSession]:
        """Find an active session by its ACS callConnectionId."""
        for session in self.active_sessions.values():
            if session.call_connection_id == call_connection_id:
                return session
        return None
        
    async def send_transcript_email(self, session_id: str, caller_phone: Optional[str] = None) -> bool:
        """
        Retrieve transcript for a session and send it via email.
        
        Args:
            session_id: Session ID to get transcript for
            caller_phone: Optional caller phone number for email context
            
        Returns:
            bool: True if email sent successfully
        """
        try:
            from utils.email_service import email_service
            import asyncio
            
            # Wait for any pending transcriptions to be saved to Cosmos DB
            # This ensures we capture the last few utterances before call ends
            await asyncio.sleep(3)
            
            # Get all transcriptions for this session
            transcript_data = await storage_logger.get_transcriptions_for_session(session_id)

            session = self.active_sessions.get(session_id)
            if session and session.office_handoff_sent:
                logger.info(f"[EMAIL] Skipping final transcript for {session_id} because office handoff email was already sent")
                return True

            if session and session.office_handoff_pending:
                logger.info(f"[EMAIL] Sending queued office handoff for {session_id} at call end")
                return await self._flush_office_handoff_email(
                    session_id=session_id,
                    caller_phone=caller_phone,
                    transcript_data=transcript_data,
                )
            
            if not transcript_data:
                logger.warning(f"[EMAIL] No transcripts for {session_id}")
                return False
            
            caller_info = {"phone": caller_phone} if caller_phone else None
            
            # --- DOCTOR-SPECIFIC EMAIL ROUTING ---
            # Determine if this should go to a specific doctor
            recipient = email_service.determine_recipient_from_transcript(transcript_data)
            if recipient:
                logger.info(f"[EMAIL] To doctor: {recipient}")
            else:
                logger.info(f"[EMAIL] To default")
            # --- END DOCTOR ROUTING ---
            
            # Send the email
            success = await email_service.send_transcript_email(
                session_id=session_id,
                transcript_data=transcript_data,
                caller_info=caller_info,
                recipient=recipient
            )
            
            if success:
                logger.info(f"[EMAIL] Sent for {session_id}")
            else:
                logger.error(f"[EMAIL] Failed for {session_id}")
            
            return success
            
        except Exception as e:
            logger.error(f"[EMAIL] Error: {e}")
            return False

    async def _flush_office_handoff_email(
        self,
        session_id: str,
        caller_phone: Optional[str] = None,
        transcript_data: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Send a queued office handoff after the call has ended."""
        async with self._session_lock:
            session = self.active_sessions.get(session_id)
            if not session:
                logger.warning(f"[OFFICE HANDOFF] Session not found while flushing: {session_id}")
                return False
            if session.office_handoff_sent:
                return True
            if session.office_handoff_in_progress:
                logger.info(f"[OFFICE HANDOFF] Flush already in progress for {session_id}")
                return True
            pending = dict(session.office_handoff_pending or {})
            if not pending:
                return False
            session.office_handoff_in_progress = True

        try:
            from utils.email_service import email_service

            session = self.active_sessions.get(session_id)
            if not caller_phone and session and session.participants:
                caller_phone = session.participants[0].get("phone_number")

            if transcript_data is None:
                transcript_data = await storage_logger.get_transcriptions_for_session(session_id)

            success = await email_service.send_office_handoff_email(
                session_id=session_id,
                reason=pending.get("reason", "other"),
                summary=pending.get("summary", "Manual office review requested."),
                urgency=pending.get("urgency", "unknown"),
                caller_phone=caller_phone,
                transcript_data=transcript_data or [],
            )

            async with self._session_lock:
                session = self.active_sessions.get(session_id)
                if session:
                    if success:
                        session.office_handoff_sent = True
                        session.office_handoff_pending = None
                    session.office_handoff_in_progress = False

            if success:
                logger.info(
                    "[OFFICE HANDOFF] Sent at call end for %s reason=%s urgency=%s",
                    session_id,
                    pending.get("reason", "other"),
                    pending.get("urgency", "unknown"),
                )
            else:
                logger.error(f"[OFFICE HANDOFF] Failed at call end for {session_id}")
            return success
        except Exception as e:
            logger.error(f"[OFFICE HANDOFF] Flush error: {e}")
            async with self._session_lock:
                session = self.active_sessions.get(session_id)
                if session:
                    session.office_handoff_in_progress = False
            return False

    async def send_office_handoff_email(
        self,
        session_id: str,
        reason: str,
        summary: str,
        urgency: str = "unknown",
    ) -> bool:
        """Queue a manual office follow-up to be emailed after the call ends."""
        handoff = {
            "reason": reason or "other",
            "summary": summary or "Manual office review requested.",
            "urgency": urgency or "unknown",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        priority = {"emergency": 4, "same_day": 3, "this_week": 2, "routine": 1, "unknown": 0}

        async with self._session_lock:
            session = self.active_sessions.get(session_id)
            if not session:
                logger.warning(f"[OFFICE HANDOFF] Cannot queue; session not found: {session_id}")
                return False
            if session.office_handoff_sent:
                logger.info(f"[OFFICE HANDOFF] Already sent for {session_id}")
                return True

            existing = session.office_handoff_pending
            if existing:
                existing_priority = priority.get(str(existing.get("urgency", "unknown")), 0)
                new_priority = priority.get(handoff["urgency"], 0)
                if new_priority >= existing_priority:
                    session.office_handoff_pending = handoff
                    logger.info(
                        "[OFFICE HANDOFF] Updated queued handoff for %s reason=%s urgency=%s",
                        session_id,
                        handoff["reason"],
                        handoff["urgency"],
                    )
                else:
                    logger.info(f"[OFFICE HANDOFF] Keeping existing queued handoff for {session_id}")
                return True

            session.office_handoff_pending = handoff
            logger.info(
                "[OFFICE HANDOFF] Queued for call end session=%s reason=%s urgency=%s",
                session_id,
                handoff["reason"],
                handoff["urgency"],
            )
            return True

    def mark_appointment_booked(self, session_id: str) -> None:
        """Record successful booking and clear non-emergency pending handoffs."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        session.appointment_booked = True
        pending = session.office_handoff_pending
        if pending and pending.get("urgency") != "emergency":
            logger.info(f"[OFFICE HANDOFF] Clearing non-emergency queued handoff after successful booking for {session_id}")
            session.office_handoff_pending = None

    def clear_recoverable_office_handoff(self, session_id: str, reason: str) -> bool:
        """Clear a queued handoff when later conversation proves the case is recoverable."""
        session = self.active_sessions.get(session_id)
        if not session or not session.office_handoff_pending:
            return False

        pending = session.office_handoff_pending
        if pending.get("urgency") == "emergency":
            return False
        if pending.get("reason") != reason:
            return False

        logger.info(
            "[OFFICE HANDOFF] Clearing recoverable queued handoff for %s reason=%s",
            session_id,
            reason,
        )
        session.office_handoff_pending = None
        return True
        
    def get_session_phonebook_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            session = self.active_sessions.get(session_id)
            if not session:
                return None
                
            return {
                "matched_caller": session.phonebook_match is not None,
                "candidate_count": len(session.phonebook_candidates or []),
                "phonebook_info": _phonebook_for_model(session.phonebook_match),
            }
        except Exception as e:
            logger.error(f"[SESSION] Phonebook info error: {e}")
            return None

    def resolve_phonebook_identity(self, session_id: str, first_name: Optional[str], last_name: Optional[str]) -> Dict[str, Any]:
        session = self.active_sessions.get(session_id)
        if not session:
            return {"matched": False, "is_new_patient": False, "status": "error", "message": "Session not found."}

        caller_phone = session.participants[0].get("phone_number") if session.participants else None
        candidate_count = len(session.phonebook_candidates or [])
        lookup = get_phonebook_lookup()

        if not lookup or not caller_phone:
            session.phonebook_match = None
            return {
                "matched": False,
                "is_new_patient": True,
                "status": "lookup_unavailable",
                "candidate_count": candidate_count,
            }

        match = lookup.lookup_by_phone_and_name(caller_phone, first_name, last_name)
        if match:
            session.phonebook_match = match.to_dict()
            return {
                "matched": True,
                "is_new_patient": False,
                "status": "matched",
                "candidate_count": candidate_count,
                "phonebook_match": _phonebook_for_model(session.phonebook_match),
            }

        fuzzy_candidates: List[Dict[str, Any]] = []
        input_last = _normalize_for_match(last_name)
        for candidate in session.phonebook_candidates or []:
            candidate_last = _normalize_for_match(candidate.get("last_name"))
            if input_last and candidate_last and input_last != candidate_last:
                continue

            first_score = _name_similarity(first_name, candidate.get("first_name"))
            last_score = _name_similarity(last_name, candidate.get("last_name"))
            if first_score >= 0.72 and last_score >= 0.90:
                fuzzy_candidates.append(
                    {
                        "first_name": candidate.get("first_name"),
                        "last_name": candidate.get("last_name"),
                        "first_name_similarity": round(first_score, 3),
                        "last_name_similarity": round(last_score, 3),
                    }
                )

        if fuzzy_candidates:
            fuzzy_candidates.sort(
                key=lambda item: (item["last_name_similarity"], item["first_name_similarity"]),
                reverse=True,
            )
            best = fuzzy_candidates[0]
            return {
                "matched": False,
                "is_new_patient": False,
                "status": "possible_name_asr_mismatch",
                "candidate_count": candidate_count,
                "possible_match": best,
                "message": (
                    "The spoken name may have been misheard by speech recognition. "
                    "Ask the caller to spell the first name letter by letter, then confirm the full name again. "
                    "Do not book until the spelling is confirmed."
                ),
            }

        session.phonebook_match = None
        return {
            "matched": False,
            "is_new_patient": True,
            "status": "new_patient",
            "candidate_count": candidate_count,
            "message": "No existing patient matched all three fields: caller phone number, first name, and last name.",
        }

    async def initialize(self):
        """Initialize the session manager and storage containers."""
        try:
            await storage_logger.initialize_containers()
            logger.info("[SESSION] Initialized")
        except Exception as e:
            logger.error(f"[SESSION] Init error: {e}")
            raise

# Global session manager instance
session_manager = SessionManager()
