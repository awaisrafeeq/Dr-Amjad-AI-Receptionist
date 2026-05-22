"""Copy all containers and documents from one Cosmos DB account to another.

Required environment variables:
  OLD_COSMOS_URI
  OLD_COSMOS_KEY
  NEW_COSMOS_URI
  NEW_COSMOS_KEY

Optional:
  OLD_COSMOS_DB_NAME / NEW_COSMOS_DB_NAME / AZURE_COSMOS_DB_NAME

The script creates target containers with the same partition key paths and
upserts every document. It is idempotent, so you can run it more than once.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

from azure.cosmos import PartitionKey, exceptions
from azure.cosmos.aio import CosmosClient


SYSTEM_PROPERTIES = {"_rid", "_self", "_etag", "_attachments", "_ts"}


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _database_names() -> tuple[str, str]:
    default = os.getenv("AZURE_COSMOS_DB_NAME", "AzureCallingAppDB")
    return (
        os.getenv("OLD_COSMOS_DB_NAME", default),
        os.getenv("NEW_COSMOS_DB_NAME", default),
    )


def _partition_key_path(container_props: Dict[str, Any]) -> str:
    paths = (container_props.get("partitionKey") or {}).get("paths") or []
    if not paths:
        raise RuntimeError(f"Container {container_props.get('id')} has no partition key path")
    return paths[0]


def _unique_key_policy(container_props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    policy = container_props.get("uniqueKeyPolicy") or {}
    unique_keys = policy.get("uniqueKeys") or []
    if not unique_keys:
        return None
    return {"uniqueKeys": unique_keys}


def _clean_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if key not in SYSTEM_PROPERTIES}


async def _ensure_database(client: CosmosClient, db_name: str):
    try:
        return await client.create_database(db_name)
    except exceptions.CosmosResourceExistsError:
        return client.get_database_client(db_name)


async def _read_container_props(database, container_id: str) -> Optional[Dict[str, Any]]:
    try:
        container = database.get_container_client(container_id)
        return await container.read()
    except exceptions.CosmosResourceNotFoundError:
        return None


async def _ensure_container(target_db, source_props: Dict[str, Any], dry_run: bool) -> None:
    container_id = source_props["id"]
    source_pk = _partition_key_path(source_props)
    target_props = await _read_container_props(target_db, container_id)

    if target_props:
        target_pk = _partition_key_path(target_props)
        if target_pk != source_pk:
            raise RuntimeError(
                f"Target container {container_id} exists with partition key {target_pk}, "
                f"but source uses {source_pk}"
            )
        print(f"[container] exists: {container_id} ({source_pk})")
        return

    print(f"[container] create: {container_id} ({source_pk})")
    if dry_run:
        return

    kwargs: Dict[str, Any] = {
        "id": container_id,
        "partition_key": PartitionKey(path=source_pk),
    }
    unique_policy = _unique_key_policy(source_props)
    if unique_policy:
        kwargs["unique_key_policy"] = unique_policy
    if "defaultTtl" in source_props:
        kwargs["default_ttl"] = source_props.get("defaultTtl")

    await target_db.create_container(**kwargs)


async def _copy_container(source_db, target_db, container_props: Dict[str, Any], dry_run: bool) -> tuple[int, int]:
    container_id = container_props["id"]
    source_container = source_db.get_container_client(container_id)
    target_container = target_db.get_container_client(container_id)
    copied = 0
    failed = 0

    print(f"[copy] start: {container_id}")
    async for item in source_container.query_items(
        query="SELECT * FROM c",
    ):
        clean_item = _clean_item(dict(item))
        if dry_run:
            copied += 1
        else:
            try:
                await target_container.upsert_item(clean_item)
                copied += 1
            except Exception as exc:
                failed += 1
                item_id = clean_item.get("id", "<missing-id>")
                print(f"[copy] failed: {container_id}/{item_id}: {exc}", file=sys.stderr)

        if copied and copied % 100 == 0:
            print(f"[copy] {container_id}: {copied} copied, {failed} failed")

    print(f"[copy] done: {container_id}: {copied} copied, {failed} failed")
    return copied, failed


async def migrate(selected_containers: Optional[Iterable[str]], dry_run: bool) -> int:
    old_uri = _required_env("OLD_COSMOS_URI")
    old_key = _required_env("OLD_COSMOS_KEY")
    new_uri = _required_env("NEW_COSMOS_URI")
    new_key = _required_env("NEW_COSMOS_KEY")
    old_db_name, new_db_name = _database_names()
    selected = set(selected_containers or [])

    source_client = CosmosClient(old_uri, credential=old_key)
    target_client = CosmosClient(new_uri, credential=new_key)

    try:
        source_db = source_client.get_database_client(old_db_name)
        if dry_run:
            target_db = target_client.get_database_client(new_db_name)
        else:
            target_db = await _ensure_database(target_client, new_db_name)

        containers: List[Dict[str, Any]] = []
        async for props in source_db.list_containers():
            if selected and props["id"] not in selected:
                continue
            containers.append(props)

        if not containers:
            print("[migrate] no containers found")
            return 1

        print(f"[migrate] source database: {old_db_name}")
        print(f"[migrate] target database: {new_db_name}")
        print(f"[migrate] containers: {', '.join(c['id'] for c in containers)}")
        print(f"[migrate] dry-run: {dry_run}")

        total_copied = 0
        total_failed = 0
        for props in containers:
            await _ensure_container(target_db, props, dry_run=dry_run)
            copied, failed = await _copy_container(source_db, target_db, props, dry_run=dry_run)
            total_copied += copied
            total_failed += failed

        print(f"[migrate] complete: {total_copied} copied, {total_failed} failed")
        return 0 if total_failed == 0 else 2
    finally:
        await source_client.close()
        await target_client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate all Cosmos DB containers from old account to new account.")
    parser.add_argument("--dry-run", action="store_true", help="Read source data and print what would be copied.")
    parser.add_argument(
        "--container",
        action="append",
        dest="containers",
        help="Copy only this container. Can be used multiple times. Defaults to all containers.",
    )
    args = parser.parse_args()
    return asyncio.run(migrate(args.containers, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
