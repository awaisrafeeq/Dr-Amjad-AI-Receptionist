from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import logging 
from datetime import datetime, timezone
from utils.document_utils import document_processor
import uuid
from models.document_model import DocumentChunk
from pydantic import BaseModel
from typing import Optional, List

# Initialize router
router = APIRouter(prefix="/documents", tags=["documents"])

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class CrawlRequest(BaseModel):
    base_url: str
    max_pages: int = 50
    allowed_domains: Optional[List[str]] = None

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
):
    """
    Upload a CSV or PDF file to Azure Blob Storage and index it in Azure AI Search
    """
    # Validate file type
    if not file.filename.lower().endswith(('.csv', '.pdf')):
        raise HTTPException(status_code=400, detail="Only CSV and PDF files are supported")
    
    processor = document_processor
    
    try:
        # Ensure search index exists
        await processor.create_search_index_if_not_exists()
        
        # Upload file to blob storage
        blob_url = await processor.upload_to_blob(file)
        
        # Reset file pointer and read content for processing
        await file.seek(0)
        file_content = await file.read()
        
        # Extract text based on file type
        if file.filename.lower().endswith('.pdf'):
            text = processor.extract_text_from_pdf(file_content)
            file_type = "pdf"
        else:  # CSV
            text = processor.extract_text_from_csv(file_content)
            file_type = "csv"
        
        # Chunk the text
        chunks = processor.chunk_text(text)
        
        # Create document chunks
        documents = []
        
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        
        for i, chunk in enumerate(chunks):
            doc = DocumentChunk(
                id=f"{uuid.uuid4()}",
                content=chunk,
                source_file=file.filename,
                chunk_index=i,
                file_type=file_type,
                upload_timestamp=timestamp,
                blob_url=blob_url
            )
            documents.append(doc)
        
        # Upload to Azure AI Search
        search_result = await processor.upload_to_search(documents)
        
        return JSONResponse(
            status_code=200,
            content={
                "message": "File uploaded and indexed successfully",
                "blob_url": blob_url,
                "chunks_created": len(chunks),
                "search_results": len(search_result),
                "file_type": file_type,
                "filename": file.filename
            }
        )
        
    except Exception as e:
        logging.error(f"Upload failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.post("/crawl")
async def crawl_website(payload: CrawlRequest):
    """Crawl a website and index its pages into Azure AI Search."""
    processor = document_processor
    try:
        pages_indexed, chunks_indexed = await processor.crawl_and_index_website(
            base_url=payload.base_url,
            max_pages=payload.max_pages,
            allowed_domains=payload.allowed_domains,
        )
        return JSONResponse(
            status_code=200,
            content={
                "message": "Website crawled and indexed successfully",
                "base_url": payload.base_url,
                "pages_indexed": pages_indexed,
                "chunks_indexed": chunks_indexed,
                "index_name": processor.azure_search_index,
            },
        )
    except Exception as e:
        logging.error(f"Crawl failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Crawl failed: {str(e)}")