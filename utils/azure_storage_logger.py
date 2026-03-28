"""
Azure cosmos Storage Logger for Call Metadata and Analytics
Handles storage of call transcriptions, histories, user intents, and operational analytics
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from uuid import uuid4
import asyncio
from azure.core.exceptions import ResourceExistsError, AzureError
from config import get_config
from azure.cosmos import PartitionKey, exceptions
from azure.cosmos.aio import CosmosClient

logger = logging.getLogger(__name__)

class AzureStorageLogger:
    def __init__(self):
        """Initialize Azure Storage Logger with configuration from environment."""
        config = get_config()
        self.cosmos_uri = config.get("azure_cosmos_uri")
        self.cosmos_key = config.get("azure_cosmos_key")
        self.database_name = config.get("azure_cosmos_db_name")
        
        # Container names for different types of data
        self.containers = {
            "call_metadata": {
                "id": "call-metadata",
                "partition_key": PartitionKey(path="/sessionId"),
                "unique_keys": [["/callId", "/correlationId"]]
            },
            "transcriptions": {
                "id": "call-transcriptions",
                "partition_key": PartitionKey(path="/sessionId"),
                "unique_keys": [["/speaker", "/timestamp", "/utterance_text"]]
            },
            # "analytics": {
            #     "id": "operational-analytics",
            #     "partition_key": PartitionKey(path="/date"),
            #     "unique_keys": [["/metricType"]]
            # },
            # "compliance": {
            #     "id": "compliance-logs",
            #     "partition_key": PartitionKey(path="/sessionId"),
            #     "unique_keys": [["/action", "/timestamp"]]
            # },
            # "user_intents": {
            #     "id": "user-intents",
            #     "partition_key": PartitionKey(path="/sessionId"),
            #     "unique_keys": [["/utteranceId"]]
            # },
            "session_logs": {
                "id": "session-logs",
                "partition_key": PartitionKey(path="/sessionId"),
                "unique_keys": [["/eventType", "/timestamp"]]
            },
            "call_histories": {
                "id": "call-histories",
                "partition_key": PartitionKey(path="/phoneNumber"),
                "unique_keys": [["/sessionId"]]
            }
        }
        
        self._cosmos_service_client = None
        
    async def get_cosmos_service_client(self) -> CosmosClient:
        """Get or create cosmos service client."""
        if self._cosmos_service_client is None:
            self._cosmos_service_client = CosmosClient(
                self.cosmos_uri, 
                credential=self.cosmos_key
            )
        return self._cosmos_service_client
        
    async def initialize_containers(self):
        """Initialize all required containers in Azure cosmos Storage."""
        client = await self.get_cosmos_service_client()

        # Create or get database
        try:
            database = await client.create_database(self.database_name)
            logger.info(f"Database '{self.database_name}' created.")
        except exceptions.ResourceExistsError:
            database = client.get_database_client(self.database_name)
            logger.info(f"Database '{self.database_name}' already exists.")

        # Create containers
        for name, container_def in self.containers.items():
            try:
                container = await database.create_container(
                    id=container_def["id"],
                    partition_key=container_def["partition_key"],
                    unique_key_policy={
                       "uniqueKeys": [{"paths": uk} for uk in container_def["unique_keys"]]
                    } 
                )
                logger.info(
                    f"Container '{container_def['id']}' created "
                    f"with unique keys {container_def['unique_keys']}."
                )
            except exceptions.ResourceExistsError:
                logger.info(f"Container '{container_def['id']}' already exists.")
            except Exception as e:
                logger.info(f"Failed to create container '{container_def['id']}': {e}")
    
    async def get_container(self, container_key: str):
        """Get container client by key."""
        if container_key not in self.containers:
            raise KeyError(
                f"Container key '{container_key}' not found. "
                f"Available containers: {list(self.containers.keys())}"
            )
        
        client = await self.get_cosmos_service_client()
        database = client.get_database_client(self.database_name)
        container_name = self.containers[container_key]['id']
        return database.get_container_client(container_name)
    
    async def log_call_metadata(self, call_data: Dict[str, Any]):
        """
        Store call metadata including basic call information.
        
        Args:
            call_data: Dictionary containing call metadata
            
        Returns:
            str: cosmos name of stored metadata
        """
        try:
            
            container = await self.get_container('call_metadata')
            await container.upsert_item(call_data)
            
            logger.info(f"Successfully logged call metadata for session {call_data.get('sessionId')}")
        
        except Exception as e:
            logger.error(f"Error logging call metadata: {e}")
            raise
            
    async def log_session_event(self, log_id:str, session_id: str, event_data: Dict[str, Any]) -> str:
        """
        Store detailed session events and state changes.
        
        Args:
            log_id: Unique log identifier
            session_id: Unique session identifier
            event_data: Dictionary containing session event data
            
        Returns:
            str: cosmos name of stored session event
        """
        try:
            
            container = await self.get_container('session_logs')
            item = {
                'id': log_id,
                'sessionId': session_id,
                **event_data  # merge event data correctly
            }
            await container.upsert_item(item)
            
        except Exception as e:
            logger.error(f"Error logging session event: {e}")
            raise
    
    async def log_transcription(self, transcription_id:str, session_id: str, transcription_data: Dict[str, Any]) -> str:
        """
        Store transcription data for a call session.
        
        Args:
            transcription_data: Dictionary containing transcription data
            
        Returns:
            str: cosmos name of stored transcription
        """
        try:
            container = await self.get_container('transcriptions')
            item = {
                'id': transcription_id,
                'sessionId': session_id,
                **transcription_data  # merge event data correctly
            }
            await container.upsert_item(item)
            
            logger.info(f"Successfully logged transcription for session {session_id}")
            
        except Exception as e:
            logger.error(f"Error logging transcription: {e}")
            raise
    
    async def log_call_history(self, history_id:str, phone_number: str, history_data: Dict[str, Any]) -> str:
        """
        Store call history data for a phone number.
        
        Args:
            history_id: Unique history identifier
            phone_number: Phone number associated with the call
            history_data: Dictionary containing call history data
        Returns:
            str: cosmos name of stored call history
        """ 
        try:
            container = await self.get_container('call_histories')
            item = {
                'id': history_id,
                'phoneNumber': phone_number,
                **history_data  # merge event data correctly
            }
            await container.upsert_item(item)
            
            logger.info(f"Successfully logged call history for phone number {phone_number}")
            
        except Exception as e:
            logger.error(f"Error logging call history: {e}")
            raise
            
    
    async def get_transcriptions_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Retrieve all transcriptions for a specific session, sorted by timestamp.
        
        Args:
            session_id: The session ID to query
            
        Returns:
            List of transcription dictionaries sorted by timestamp
        """
        try:
            container = await self.get_container('transcriptions')
            
            # Query for all transcriptions with this session ID
            query = "SELECT * FROM c WHERE c.sessionId = @sessionId ORDER BY c.timestamp ASC"
            parameters = [{"name": "@sessionId", "value": session_id}]
            
            transcriptions = []
            async for item in container.query_items(
                query=query,
                parameters=parameters,
                partition_key=session_id
            ):
                transcriptions.append({
                    "speaker": item.get("speaker", "unknown"),
                    "utterance_text": item.get("utterance_text", ""),
                    "timestamp": item.get("timestamp", "")
                })
            
            logger.info(f"Retrieved {len(transcriptions)} transcriptions for session {session_id}")
            return transcriptions
            
        except Exception as e:
            logger.error(f"Error retrieving transcriptions for session {session_id}: {e}")
            return []

    async def close(self):
        """Close the cosmos service client."""
        if self._cosmos_service_client:
            await self._cosmos_service_client.close()


# Global instance
storage_logger = AzureStorageLogger()