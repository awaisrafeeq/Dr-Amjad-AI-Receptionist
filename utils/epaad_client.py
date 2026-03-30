import os
import time
import aiohttp
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EpaadConfig:
    base_url: str
    username: str
    password: str


class EpaadClient:
    def __init__(self, config: Optional[EpaadConfig] = None):
        self._config = config or EpaadConfig(
            base_url=os.getenv("EPAAD_BASE_URL", "https://mcv.epaad.ch").rstrip("/"),
            username=os.getenv("EPAAD_USERNAME", ""),
            password=os.getenv("EPAAD_PASSWORD", ""),
        )

        self._token: Optional[str] = None
        self._token_acquired_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # Token is assumed to be valid for ~60 minutes per client guidance.
        self._token_ttl_seconds = int(os.getenv("EPAAD_TOKEN_TTL_SECONDS", "3540"))  # 59 minutes

    def _auth_headers(self) -> Dict[str, str]:
        if not self._token:
            return {"Accept": "application/json"}
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _authenticate(self, session: aiohttp.ClientSession) -> str:
        url = f"{self._config.base_url}/api/v1/authenticate"
        logger.info(f"[EPAAD AUTH] Authenticating with {url}")
        async with session.post(
            url,
            data={"username": self._config.username, "password": self._config.password},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            body = await resp.json(content_type=None)
            logger.info(f"[EPAAD AUTH] Response status={resp.status}")
            if resp.status not in (200, 201):
                logger.error(f"[EPAAD AUTH ERROR] status={resp.status}, body={body}")
                raise RuntimeError(f"EPAAD authenticate failed (status={resp.status}): {body}")

            token = None
            if isinstance(body, dict):
                token = body.get("auth_token") or body.get("token") or body.get("access_token")
            if not token:
                logger.error(f"[EPAAD AUTH ERROR] No token found in response: {body}")
                raise RuntimeError("EPAAD authenticate succeeded but no token field found")

            self._token = token
            self._token_acquired_at = time.time()
            logger.info(f"[EPAAD AUTH] Token acquired successfully (expires in {self._token_ttl_seconds}s)")
            return token

    async def _ensure_token(self, session: aiohttp.ClientSession) -> None:
        if self._token and (time.time() - self._token_acquired_at) < self._token_ttl_seconds:
            return

        async with self._token_lock:
            if self._token and (time.time() - self._token_acquired_at) < self._token_ttl_seconds:
                return
            await self._authenticate(session)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        async with aiohttp.ClientSession() as session:
            await self._ensure_token(session)
            url = f"{self._config.base_url}{path}"
            
            # DEBUG: Log request details
            logger.info(f"[EPAAD API REQUEST] {method} {url}")
            import json
            logger.info(f"[EPAAD API REQUEST] params={params}, body={json.dumps(json_body) if json_body else None}")

            async with session.request(
                method,
                url,
                headers=self._auth_headers(),
                params=params,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                # If token expired earlier than expected
                if resp.status == 401:
                    logger.warning(f"[EPAAD API] Token expired (401), re-authenticating...")
                    async with self._token_lock:
                        self._token = None
                        self._token_acquired_at = 0.0
                    await self._ensure_token(session)
                    async with session.request(
                        method,
                        url,
                        headers=self._auth_headers(),
                        params=params,
                        json=json_body,
                        timeout=aiohttp.ClientTimeout(total=25),
                    ) as resp2:
                        data2 = await resp2.json(content_type=None)
                        logger.info(f"[EPAAD API RESPONSE] status={resp2.status}, has_data={data2 is not None}")
                        if resp2.status >= 400:
                            logger.error(f"[EPAAD API ERROR] status={resp2.status}, data={data2}")
                            raise RuntimeError(f"EPAAD request failed (status={resp2.status}): {data2}")
                        return data2

                if resp.status == 204:
                    logger.info(f"[EPAAD API RESPONSE] status=204 (No Content)")
                    return None

                data = await resp.json(content_type=None)
                if resp.status == 201 and "events" in url:
                    logger.info(f"[EPAAD API RESPONSE] status={resp.status}, body={data}")
                else:
                    logger.info(f"[EPAAD API RESPONSE] status={resp.status}, data_type={type(data).__name__}, keys={list(data.keys())[:5] if isinstance(data, dict) else 'N/A'}")
                if resp.status >= 400:
                    logger.error(f"[EPAAD API ERROR] status={resp.status}, data={data}")
                    raise RuntimeError(f"EPAAD request failed (status={resp.status}): {data}")
                return data

    async def get_calendars(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/api/v1/onedoc/calendars")

    async def get_events(self, calendar_id: int, from_dt: str, until_dt: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/api/v1/onedoc/calendars/{calendar_id}/events",
            params={"fromDateTime": from_dt, "untilDateTime": until_dt},
        )

    async def get_event(self, calendar_id: int, event_id: int, event_type: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/onedoc/calendars/{calendar_id}/events/{event_id}",
            params={"type": event_type},
        )

    async def create_event(self, calendar_id: int, event: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/onedoc/calendars/{calendar_id}/events",
            json_body=event,
        )

    async def delete_event(self, calendar_id: int, onedoc_key: str) -> None:
        await self._request(
            "DELETE",
            f"/api/v1/onedoc/calendars/{calendar_id}/events/{onedoc_key}",
        )

    async def get_event_changes(self, after_change_id: Optional[int] = None) -> Any:
        params: Dict[str, Any] = {}
        if after_change_id is not None:
            params["afterChangeId"] = after_change_id
        return await self._request("GET", "/api/v1/onedoc/events/changes", params=params)

    async def get_latest_event_change_id(self) -> Any:
        return await self._request("GET", "/api/v1/onedoc/events/last-change-id")


epaad_client = EpaadClient()
