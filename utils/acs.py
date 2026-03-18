from azure.communication.callautomation import (
    CallAutomationClient,
    MediaStreamingOptions,
    StreamingTransportType,
    MediaStreamingContentType,
    MediaStreamingAudioChannelType,
    AudioFormat
)
import asyncio
from config import get_config

config = get_config()

class AcsCaller:
    
    source_number: str
    acs_connection_string: str
    acs_callback_path: str
    acs_media_streaming_websocket_path: str
    cognitive_services_endpoint: str
    media_streaming_configuration: MediaStreamingOptions
    
    def __init__(self):
        self.source_number = config["acs_source_number"]
        self.acs_connection_string = config["acs_connection_string"]
        self.acs_callback_path = config["acs_callback_path"]
        self.acs_media_streaming_websocket_path = config["acs_media_streaming_ws"]
        self.cognitive_services_endpoint = config["cognitive_service_endpoint"]
        self.media_streaming_configuration = MediaStreamingOptions(
            transport_url=self.acs_media_streaming_websocket_path,
            transport_type=StreamingTransportType.WEBSOCKET,
            content_type=MediaStreamingContentType.AUDIO,
            audio_channel_type=MediaStreamingAudioChannelType.MIXED,
            start_media_streaming=True,
            enable_bidirectional=True,
            audio_format=AudioFormat.PCM24_K_MONO
        )

    async def answer_inbound_call(self, incoming_call_context: str, callback_uri: str):
        client = CallAutomationClient.from_connection_string(self.acs_connection_string)

        def _answer_call_sync():
            kwargs = {
                "incoming_call_context": incoming_call_context,
                "media_streaming": self.media_streaming_configuration,
                "callback_url": callback_uri,
                "operation_context": "incomingCall",
            }

            # Passing a wrong endpoint can cause AnswerFailed (404/1600). Only pass when it looks like
            # a Cognitive Services *resource* endpoint.
            endpoint = (self.cognitive_services_endpoint or "").strip()
            endpoint_lower = endpoint.lower()
            if endpoint and (
                "cognitiveservices.azure.com" in endpoint_lower
                or ".api.cognitive.microsoft.com" in endpoint_lower
            ):
                kwargs["cognitive_services_endpoint"] = endpoint

            return client.answer_call(**kwargs)

        return await asyncio.to_thread(_answer_call_sync)

acs_caller = AcsCaller()
