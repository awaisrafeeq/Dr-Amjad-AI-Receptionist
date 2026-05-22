"""
Session Manager for tracking call sessions and coordinating logging
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from uuid import uuid4
from dataclasses import dataclass, asdict, field
from utils.azure_storage_logger import storage_logger
from utils.phonebook_lookup import get_phonebook_lookup

logger = logging.getLogger(__name__)


def _phonebook_for_model(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    return {key: (value if value not in (None, "") else "MISSING") for key, value in data.items()}

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
    insurance_card_number: Optional[str] = None

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

    async def restore_session(self, session_id: Optional[str]) -> Optional[CallSession]:
        """Restore a session from persisted metadata after an app restart."""
        if not session_id:
            return None

        async with self._session_lock:
            existing = self.active_sessions.get(session_id)
            if existing:
                return existing

        metadata = await storage_logger.get_call_metadata(session_id)
        if not metadata:
            return None

        stored_status = (metadata.get("status") or "").lower()
        if stored_status in {"disconnected", "completed", "failed"}:
            logger.info(
                f"[SESSION] Not restoring ended session {session_id[:8]} status={stored_status}"
            )
            return None

        def _parse_datetime(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str) and value:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    pass
            return datetime.now(timezone.utc)

        phonebook_candidates: List[Dict[str, Any]] = []
        try:
            participants = metadata.get("participants") or []
            caller_phone = participants[0].get("phone_number") if participants else None
            lookup = get_phonebook_lookup()
            if lookup is not None and caller_phone:
                phonebook_candidates = [
                    match.to_dict() for match in lookup.lookup_candidates_by_phone(caller_phone)
                ]
        except Exception as e:
            logger.warning("Phonebook restore lookup failed (internal only): %s", e)

        session = CallSession(
            session_id=session_id,
            history_id=metadata.get("history_id") or str(uuid4()),
            start_time=_parse_datetime(metadata.get("start_time")),
            participants=metadata.get("participants") or [],
            direction=metadata.get("direction") or "inbound",
            platform=metadata.get("platform") or "Azure Communication Services",
            status=metadata.get("status") or "active",
            end_time=_parse_datetime(metadata.get("end_time")) if metadata.get("end_time") else None,
            total_duration=metadata.get("total_duration") or 0.0,
            call_connection_id=metadata.get("CallConnectionId") or metadata.get("callConnectionId"),
            server_call_id=metadata.get("ServerCallId") or metadata.get("serverCallId"),
            correlation_id=metadata.get("CorrelationId") or metadata.get("correlationId"),
            transcription_count=metadata.get("total_transcriptions") or 0,
            phonebook_candidates=phonebook_candidates,
        )

        async with self._session_lock:
            self.active_sessions[session_id] = session

        logger.warning(
            f"[SESSION] Restored from storage after restart: {session_id[:8]} "
            f"status={session.status}, call_connection_id={session.call_connection_id}"
        )
        return session
            
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
                session = self.active_sessions.get(session_id) if session_id else None

            if not session and session_id:
                session = await self.restore_session(session_id)

            if not session:
                logger.warning(f"[SESSION] Not found: {session_id}")
                self._cleanup_in_progress.discard(session_id)
                return False
            
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

            if not session_data:
                session_data = await self.restore_session(session_id)
            
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

                if session_data and any(
                    key in event_data for key in ("callConnectionId", "serverCallId", "correlationId")
                ):
                    try:
                        await storage_logger.log_call_metadata({
                            'id': session_id,
                            'sessionId': session_id,
                            'history_id': session_data.history_id,
                            'start_time': session_data.start_time.isoformat(),
                            'end_time': session_data.end_time.isoformat() if session_data.end_time else None,
                            'total_duration': session_data.total_duration,
                            'CallConnectionId': session_data.call_connection_id,
                            'ServerCallId': session_data.server_call_id,
                            'CorrelationId': session_data.correlation_id,
                            'direction': session_data.direction,
                            'participants': session_data.participants,
                            'total_transcriptions': session_data.transcription_count,
                            'platform': session_data.platform,
                            'status': session_data.status,
                        })
                    except Exception as metadata_error:
                        logger.warning(f"[SESSION] Metadata refresh skipped: {metadata_error}")
                    
                
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
                self.active_sessions[session_id].transcription_count += 1
            
            logger.debug(f"[TRANSCRIPT] {session_id[:8]}: {speaker} - {utterance_text[:30]}...")
            return transcription_id
            
        except Exception as e:
            logger.error(f"[TRANSCRIPT] Error: {e}")
            raise
    
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
            
            if not transcript_data:
                logger.warning(f"[EMAIL] No transcripts for {session_id}")
                return False
            
            caller_info = {"phone": caller_phone} if caller_phone else None

            # Attach insurance card number if collected during the call
            session = self.active_sessions.get(session_id)
            if session and session.insurance_card_number:
                if caller_info is None:
                    caller_info = {}
                caller_info["insurance_card_number"] = session.insurance_card_number
            
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

    async def send_escalation_email(
        self,
        session_id: str,
        reason: str,
        caller_phone: Optional[str] = None,
        action: str = "Please review the transcript and call the patient back if needed.",
    ) -> bool:
        """Send a staff action email without ending the session."""
        try:
            from utils.email_service import email_service

            if not session_id:
                return False

            transcript_data = await storage_logger.get_transcriptions_for_session(session_id)
            session = self.active_sessions.get(session_id)
            if caller_phone is None and session and session.participants:
                caller_phone = session.participants[0].get("phone_number")

            caller_info = {"phone": caller_phone} if caller_phone else None
            success = await email_service.send_escalation_email(
                session_id=session_id,
                transcript_data=transcript_data or [],
                caller_info=caller_info,
                reason=reason,
                action=action,
            )
            if success:
                logger.info(f"[ESCALATION EMAIL] Sent for {session_id}: {reason}")
            else:
                logger.error(f"[ESCALATION EMAIL] Failed for {session_id}: {reason}")
            return success
        except Exception as e:
            logger.error(f"[ESCALATION EMAIL] Error: {e}")
            return False
        
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
