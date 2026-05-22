"""Backup and restore one Azure AI Search index across services.

Required for export:
  OLD_SEARCH_ENDPOINT
  OLD_SEARCH_KEY

Required for import:
  NEW_SEARCH_ENDPOINT
  NEW_SEARCH_KEY

Optional:
  SEARCH_INDEX_NAME (default: aicallerknowledgebaseindex)
  SEARCH_BACKUP_DIR (default: search_backup)

Examples:
  python scripts/migrate_search_index.py export
  python scripts/migrate_search_index.py import
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List

from azure.core.exceptions import HttpResponseError
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import SearchIndex


def _env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "")
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _index_name() -> str:
    return os.getenv("SEARCH_INDEX_NAME", "aicallerknowledgebaseindex")


def _backup_dir() -> Path:
    return Path(os.getenv("SEARCH_BACKUP_DIR", "search_backup"))


def _old_clients(index_name: str) -> tuple[SearchIndexClient, SearchClient]:
    endpoint = _env("OLD_SEARCH_ENDPOINT")
    key = _env("OLD_SEARCH_KEY")
    credential = AzureKeyCredential(key)
    return (
        SearchIndexClient(endpoint=endpoint, credential=credential),
        SearchClient(endpoint=endpoint, index_name=index_name, credential=credential),
    )


def _new_clients(index_name: str) -> tuple[SearchIndexClient, SearchClient]:
    endpoint = _env("NEW_SEARCH_ENDPOINT")
    key = _env("NEW_SEARCH_KEY")
    credential = AzureKeyCredential(key)
    return (
        SearchIndexClient(endpoint=endpoint, credential=credential),
        SearchClient(endpoint=endpoint, index_name=index_name, credential=credential),
    )


def _model_to_dict(model: Any) -> Dict[str, Any]:
    if hasattr(model, "as_dict"):
        return model.as_dict()
    return json.loads(json.dumps(model))


def _strip_search_metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in doc.items()
        if not key.startswith("@search.")
    }


def _chunks(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _legacy_vector_index_payload(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Convert SDK-exported snake_case schema to legacy REST vector schema."""
    fields: List[Dict[str, Any]] = []
    vector_config_name = "my-vector-config"

    for field in schema.get("fields", []):
        item: Dict[str, Any] = {
            "name": field["name"],
            "type": field["type"],
        }
        for prop in ("key", "searchable", "filterable", "sortable", "facetable", "retrievable"):
            if prop in field:
                item[prop] = field[prop]

        if field.get("type") == "Collection(Edm.Single)":
            vector_config_name = (
                field.get("vector_search_profile_name")
                or field.get("vectorSearchConfiguration")
                or vector_config_name
            )
            item["dimensions"] = field.get("vector_search_dimensions") or field.get("dimensions") or 1536
            item["vectorSearchConfiguration"] = vector_config_name

        fields.append(item)

    algorithm = {}
    algorithms = (schema.get("vector_search") or {}).get("algorithms") or []
    if algorithms:
        algorithm = algorithms[0]
    parameters = algorithm.get("parameters") or {}

    payload: Dict[str, Any] = {
        "name": schema["name"],
        "fields": fields,
        "vectorSearch": {
            "algorithmConfigurations": [
                {
                    "name": vector_config_name,
                    "kind": algorithm.get("kind", "hnsw"),
                    "hnswParameters": {
                        "m": parameters.get("m", 4),
                        "efConstruction": parameters.get("ef_construction", parameters.get("efConstruction", 400)),
                        "efSearch": parameters.get("ef_search", parameters.get("efSearch", 500)),
                        "metric": parameters.get("metric", "cosine"),
                    },
                }
            ]
        },
    }

    semantic = schema.get("semantic_search") or {}
    configurations = semantic.get("configurations") or []
    if configurations:
        converted = []
        for config in configurations:
            prioritized = config.get("prioritized_fields") or {}
            converted.append(
                {
                    "name": config.get("name", "default"),
                    "prioritizedFields": {
                        "prioritizedContentFields": [
                            {"fieldName": field.get("field_name")}
                            for field in prioritized.get("content_fields", [])
                            if field.get("field_name")
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": field.get("field_name")}
                            for field in prioritized.get("keywords_fields", [])
                            if field.get("field_name")
                        ],
                    },
                }
            )
        payload["semantic"] = {"configurations": converted}

    return payload


def _create_index_with_legacy_rest(schema: Dict[str, Any]) -> None:
    endpoint = _env("NEW_SEARCH_ENDPOINT").rstrip("/")
    key = _env("NEW_SEARCH_KEY")
    index_name = schema["name"]
    payload = _legacy_vector_index_payload(schema)
    body = json.dumps(payload).encode("utf-8")
    url = f"{endpoint}/indexes/{index_name}?api-version=2023-07-01-Preview"
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "api-key": key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"[import] legacy REST index create status: {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Legacy REST index create failed: HTTP {exc.code}: {detail}") from exc


def export_index() -> None:
    index_name = _index_name()
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    index_client, search_client = _old_clients(index_name)
    index = index_client.get_index(index_name)
    index_path = backup_dir / f"{index_name}.schema.json"
    docs_path = backup_dir / f"{index_name}.documents.jsonl"

    index_path.write_text(json.dumps(_model_to_dict(index), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[export] schema: {index_path}")

    count = 0
    with docs_path.open("w", encoding="utf-8") as handle:
        results = search_client.search(search_text="*", top=1000, include_total_count=True)
        total = results.get_count()
        if total is not None:
            print(f"[export] expected documents: {total}")
        for result in results:
            doc = _strip_search_metadata(dict(result))
            handle.write(json.dumps(doc, ensure_ascii=False) + "\n")
            count += 1
            if count % 100 == 0:
                print(f"[export] documents: {count}")

    print(f"[export] documents: {docs_path}")
    print(f"[export] complete: {count} documents")


def import_index(delete_existing: bool) -> None:
    index_name = _index_name()
    backup_dir = _backup_dir()
    index_path = backup_dir / f"{index_name}.schema.json"
    docs_path = backup_dir / f"{index_name}.documents.jsonl"

    if not index_path.exists() or not docs_path.exists():
        raise RuntimeError(f"Backup files not found in {backup_dir}")

    index_client, search_client = _new_clients(index_name)
    schema = json.loads(index_path.read_text(encoding="utf-8"))
    index_model = SearchIndex.deserialize(schema)

    if delete_existing:
        try:
            index_client.delete_index(index_name)
            print(f"[import] deleted existing index: {index_name}")
            time.sleep(3)
        except Exception:
            pass

    print(f"[import] create/update index: {index_name}")
    try:
        index_client.create_or_update_index(index_model)
    except HttpResponseError as exc:
        if "vector field 'content_vector'" not in str(exc):
            raise
        print("[import] SDK schema create hit vector API mismatch; using legacy REST fallback")
        _create_index_with_legacy_rest(schema)
    time.sleep(3)

    batch: List[Dict[str, Any]] = []
    total = 0
    failed = 0
    with docs_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= 100:
                results = search_client.upload_documents(batch)
                total += len(batch)
                failed += sum(1 for item in results if not item.succeeded)
                print(f"[import] uploaded: {total}, failed: {failed}")
                batch = []

    if batch:
        results = search_client.upload_documents(batch)
        total += len(batch)
        failed += sum(1 for item in results if not item.succeeded)

    print(f"[import] complete: {total} uploaded, {failed} failed")
    if failed:
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export/import an Azure AI Search index.")
    parser.add_argument("command", choices=["export", "import"])
    parser.add_argument("--delete-existing", action="store_true", help="Delete target index before import.")
    args = parser.parse_args()

    if args.command == "export":
        export_index()
    else:
        import_index(delete_existing=args.delete_existing)


if __name__ == "__main__":
    main()
