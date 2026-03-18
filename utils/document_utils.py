from fastapi import APIRouter, UploadFile, HTTPException
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import AzureError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField
)
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI
import aiohttp
import pandas as pd
import PyPDF2
import io
import uuid
from typing import List, Optional, Set, Tuple
import logging 
from config import get_config
from models.document_model import DocumentChunk
from urllib.parse import urljoin, urlparse
import re

try:
    from langdetect import detect as detect_lang
except Exception:  # pragma: no cover
    detect_lang = None

# --- ENV VARS ---
config = get_config()

class DocumentProcessor:
    
    blob_service_client: BlobServiceClient
    openai_client: AzureOpenAI
    search_index_client: SearchIndexClient
    search_client: SearchClient
    azure_blob_container: str
    azure_search_index: str
    azure_openai_embedding_deployment: str
    
    
    def __init__(self):
        self.blob_service_client = BlobServiceClient.from_connection_string(
            config['azure_blob_conn']
        )
        self.openai_client = AzureOpenAI(
            azure_endpoint=config['azure_openai_endpoint'],
            api_key=config['azure_openai_key'],
            api_version='2024-02-01',
            
        )
        self.search_index_client = SearchIndexClient(
            endpoint=config['azure_search_endpoint'],
            credential=AzureKeyCredential(config['azure_search_key'])
        )
        self.search_client = SearchClient(
            endpoint=config['azure_search_endpoint'],
            index_name='aicallerknowledgebaseindex',
            credential=AzureKeyCredential(config['azure_search_key'])
        )
        
        self.azure_blob_container = "aicallerknowledgebase"
        self.azure_search_index = "aicallerknowledgebaseindex"
        self.azure_openai_embedding_deployment = config['azure_openai_embedding_deployment']

    def _detect_language(self, text: str) -> Optional[str]:
        if not text:
            return None
        if detect_lang is None:
            return None
        try:
            return detect_lang(text)
        except Exception:
            return None

    def _html_to_text(self, html: str) -> str:
        if not html:
            return ""

        cleaned = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\\1>", " ", html)
        cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _extract_links(self, html: str, base_url: str) -> List[str]:
        if not html:
            return []
        links = re.findall(r"(?i)href=['\"](.*?)['\"]", html)
        out: List[str] = []
        for href in links:
            if not href:
                continue
            if href.startswith("mailto:") or href.startswith("tel:"):
                continue
            if href.startswith("javascript:"):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            normalized = parsed._replace(fragment="").geturl()
            out.append(normalized)
        return out

    async def create_search_index_if_not_exists(self):
        """Create the search index if it doesn't exist"""
        try:
            # Check if index exists
            try:
                existing_index = self.search_index_client.get_index(self.azure_search_index)
            except Exception:
                existing_index = None  # Index doesn't exist

            # Define the search index schema
            fields = [
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SearchableField(name="content", type=SearchFieldDataType.String),
                SimpleField(name="source_file", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="source_url", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True),
                SimpleField(name="file_type", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="language", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="upload_timestamp", type=SearchFieldDataType.DateTimeOffset, filterable=True),
                SimpleField(name="blob_url", type=SearchFieldDataType.String),
                SearchField(
                    name="content_vector",
                    type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True,
                    vector_search_dimensions=1536,  # text-embedding-3-small dimensions
                    vector_search_profile_name="my-vector-config"
                )
            ]

            # Configure vector search
            vector_search = VectorSearch(
                algorithms=[
                    HnswAlgorithmConfiguration(name="my-hnsw")
                ],
                profiles=[
                    VectorSearchProfile(
                        name="my-vector-config",
                        algorithm_configuration_name="my-hnsw"
                    )
                ]
            )

            # Create or update the search index
            index = SearchIndex(
                name=self.azure_search_index,
                fields=fields,
                vector_search=vector_search
            )

            if existing_index is None:
                self.search_index_client.create_index(index)
                logging.info(f"Created search index: {self.azure_search_index}")
                return

            existing_field_names = {f.name for f in getattr(existing_index, "fields", []) if getattr(f, "name", None)}
            desired_field_names = {f.name for f in fields}

            if not desired_field_names.issubset(existing_field_names):
                self.search_index_client.create_or_update_index(index)
                logging.info(f"Updated search index schema: {self.azure_search_index}")
            return

        except Exception as e:
            logging.error(f"Failed to create search index: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to create search index: {str(e)}")

    async def upload_to_blob(self, file: UploadFile) -> str:
        """Upload file to Azure Blob Storage"""
        try:
            # Generate unique blob name
            blob_name = f"{uuid.uuid4()}_{file.filename}"
            
            # Create container if it doesn't exist
            container_client = self.blob_service_client.get_container_client(self.azure_blob_container)
            try:
                container_client.create_container()
            except:
                pass  # Container already exists

            # Upload file
            blob_client = container_client.get_blob_client(blob_name)
            file_content = await file.read()
            blob_client.upload_blob(file_content, overwrite=True)
            
            # Return blob URL
            return blob_client.url

        except AzureError as e:
            logging.error(f"Failed to upload to blob storage: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")

    def extract_text_from_pdf(self, file_content: bytes) -> str:
        """Extract text from PDF file"""
        try:
            pdf_file = io.BytesIO(file_content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            
            return text.strip()
        except Exception as e:
            logging.error(f"Failed to extract text from PDF: {str(e)}")
            raise HTTPException(status_code=400, detail="Failed to process PDF file")

    def extract_text_from_csv(self, file_content: bytes) -> str:
        """Extract text from CSV file"""
        try:
            csv_file = io.StringIO(file_content.decode('utf-8'))
            df = pd.read_csv(csv_file)
            
            # Convert DataFrame to text representation
            text = df.to_string(index=False)
            return text
        except Exception as e:
            logging.error(f"Failed to extract text from CSV: {str(e)}")
            raise HTTPException(status_code=400, detail="Failed to process CSV file")

    def chunk_text(self, text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
        """Split text into chunks for embedding"""
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            if end > len(text):
                end = len(text)
            
            chunk = text[start:end]
            chunks.append(chunk)
            
            if end == len(text):
                break
                
            start = end - overlap
        
        return chunks

    async def create_embedding(self, text: str) -> List[float]:
        """Create embedding using Azure OpenAI"""
        try:
            response = self.openai_client.embeddings.create(
                model=self.azure_openai_embedding_deployment,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            logging.error(f"Failed to create embedding: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to create embedding: {str(e)}")

    async def upload_to_search(self, documents: List[DocumentChunk]):
        """Upload documents to Azure AI Search"""
        try:
            # Create embeddings for all documents
            search_documents = []
            
            for doc in documents:
                embedding = await self.create_embedding(doc.content)
                
                search_doc = {
                    "id": doc.id,
                    "content": doc.content,
                    "source_file": doc.source_file,
                    "source_url": doc.source_url,
                    "chunk_index": doc.chunk_index,
                    "file_type": doc.file_type,
                    "language": doc.language,
                    "upload_timestamp": doc.upload_timestamp,
                    "blob_url": doc.blob_url,
                    "content_vector": embedding
                }
                search_documents.append(search_doc)
            
            # Upload to search index
            result = self.search_client.upload_documents(search_documents)
            return result
            
        except Exception as e:
            logging.error(f"Failed to upload to search index: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to upload to search index: {str(e)}")

    async def crawl_and_index_website(
        self,
        base_url: str,
        max_pages: int = 50,
        allowed_domains: Optional[List[str]] = None,
    ) -> Tuple[int, int]:
        await self.create_search_index_if_not_exists()

        parsed_base = urlparse(base_url)
        if parsed_base.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="base_url must be http/https")

        if allowed_domains is None:
            allowed_domains = [parsed_base.netloc]

        to_visit: List[str] = [base_url]
        visited: Set[str] = set()

        pages_indexed = 0
        chunks_indexed = 0

        async with aiohttp.ClientSession() as session:
            while to_visit and pages_indexed < max_pages:
                url = to_visit.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                parsed = urlparse(url)
                if parsed.netloc not in allowed_domains:
                    continue

                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=20),
                        headers={"User-Agent": "aicallerknowledgebasebot/1.0"},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        content_type = (resp.headers.get("Content-Type") or "").lower()
                        if "text/html" not in content_type:
                            continue
                        html = await resp.text(errors="ignore")
                except Exception:
                    continue

                text = self._html_to_text(html)
                if len(text) < 200:
                    links = self._extract_links(html, url)
                    for link in links:
                        if link not in visited and link not in to_visit:
                            to_visit.append(link)
                    continue

                language = self._detect_language(text)

                chunks = self.chunk_text(text)
                documents: List[DocumentChunk] = []

                from datetime import datetime, timezone
                timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

                for i, chunk in enumerate(chunks):
                    documents.append(
                        DocumentChunk(
                            id=f"{uuid.uuid4()}",
                            content=chunk,
                            source_file=base_url,
                            source_url=url,
                            chunk_index=i,
                            file_type="website",
                            language=language,
                            upload_timestamp=timestamp,
                            blob_url=url,
                        )
                    )

                await self.upload_to_search(documents)
                pages_indexed += 1
                chunks_indexed += len(chunks)

                links = self._extract_links(html, url)
                for link in links:
                    if link not in visited and link not in to_visit:
                        to_visit.append(link)

        return pages_indexed, chunks_indexed

    async def search_knowledge_base(
        self,
        query: str,
        language: Optional[str] = None,
        k: int = 5,
    ) -> List[dict]:
        if not query:
            return []

        from azure.search.documents.models import VectorizedQuery

        await self.create_search_index_if_not_exists()
        embedding = await self.create_embedding(query)

        filter_expr = None
        if language:
            safe_lang = language.replace("'", "")
            filter_expr = f"language eq '{safe_lang}'"

        results = self.search_client.search(
            search_text=None,
            vector_queries=[
                VectorizedQuery(vector=embedding, k_nearest_neighbors=k, fields="content_vector")
            ],
            filter=filter_expr,
            select=["content", "source_url", "source_file", "language"],
            top=k,
        )

        out: List[dict] = []
        for r in results:
            out.append(
                {
                    "content": r.get("content"),
                    "source_url": r.get("source_url"),
                    "source_file": r.get("source_file"),
                    "language": r.get("language"),
                }
            )
        return out
        
document_processor = DocumentProcessor()