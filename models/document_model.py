from pydantic import BaseModel
from typing import Optional

class DocumentChunk(BaseModel):
    id: str
    content: str
    source_file: str
    source_url: Optional[str] = None
    chunk_index: int
    file_type: str
    language: Optional[str] = None
    upload_timestamp: str
    blob_url: str