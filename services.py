import asyncio
import re
from pathlib import Path
from typing import List
from fastapi import HTTPException, status
from langchain_community.document_loaders import (
    CSVLoader,
    PyMuPDFLoader,
    UnstructuredMarkdownLoader,
    UnstructuredWordDocumentLoader,
)
from langchain_core.documents import Document
from langchain_core.messages import messages_from_dict
from langchain_core.output_parsers import StrOutputParser
from langchain_excel_loader import StructuredExcelLoader
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from config import (
    llm,
    logger,
    supabase_client,
    vector_store,
)

def _extract_header_metadata(text: str):
    version_match = re.search(r"(?:Version|V|POL-[\w-]+-V)[\s:]*([0-9.]+)", text, re.IGNORECASE)
    date_match = re.search(r"Effective Date:\s*([^\n\r*]+)", text, re.IGNORECASE)
    version = version_match.group(1).strip() if version_match else "v1.0"
    effective_date = date_match.group(1).strip() if date_match else "2026-01-01"
    return version, effective_date

def _load_and_chunk_documents(file_paths: List[str]) -> List[Document]:
    loader_mapping = {
        ".pdf": PyMuPDFLoader,
        ".csv": CSVLoader,
        ".tsv": CSVLoader,
        ".xlsx": StructuredExcelLoader,
        ".xls": StructuredExcelLoader,
        ".docx": UnstructuredWordDocumentLoader,
        ".doc": UnstructuredWordDocumentLoader,
        ".md": UnstructuredMarkdownLoader,
        ".markdown": UnstructuredMarkdownLoader,
    }
    
    default_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150,
        length_function=len,
    )
    
    markdown_header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
        ],
        strip_headers=False,
    )

    chunks: List[Document] = []
    for path_str in file_paths:
        file_path = Path(path_str)
        ext = file_path.suffix.lower()
        if ext not in loader_mapping:
            continue

        try:
            if ext in [".md", ".markdown"]:
                raw_text = file_path.read_text(encoding="utf-8")
                version, effective_date = _extract_header_metadata(raw_text)
                docs = markdown_header_splitter.split_text(raw_text)
            else:
                loader = loader_mapping[ext](str(file_path))
                docs = loader.load()
                combined_sample = "\n".join(d.page_content[:500] for d in docs[:3])
                version, effective_date = _extract_header_metadata(combined_sample)

            split_chunks = default_splitter.split_documents(docs)
            for chunk in split_chunks:
                chunk.metadata["source"] = file_path.name
                chunk.metadata["version"] = version
                chunk.metadata["effective_date"] = effective_date
                chunk.metadata["status"] = "active"
                chunk.page_content = f"Document: {file_path.name} | Effective Date: {effective_date} | Version: {version}\n{chunk.page_content}"
            chunks.extend(split_chunks)
        except Exception as e:
            logger.error(f"Failed to process file {file_path.name}: {e}")
            raise

    return chunks

def _store_chunks(file_paths: List[str], batch_size: int = 100) -> int:
    chunks = _load_and_chunk_documents(file_paths)
    total_chunks = len(chunks)
    if total_chunks == 0:
        return 0

    for path_str in file_paths:
        name = Path(path_str).name
        try:
            supabase_client.table("documents").update({"metadata": {"source": name, "status": "archived"}}).eq("metadata->>source", name).execute()
        except Exception:
            pass

    for i in range(0, total_chunks, batch_size):
        batch = chunks[i : i + batch_size]
        vector_store.add_documents(documents=batch)

    return total_chunks