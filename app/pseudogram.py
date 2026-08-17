import asyncio
import logging
import httpx
from typing import Optional, Dict, Any
from app.config import API_KEY, PSEUDOGRAM_BASE_URL

logger = logging.getLogger(__name__)

class PseudoGramClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or PSEUDOGRAM_BASE_URL).rstrip("/")
        self.api_key = api_key or API_KEY

    def _get_headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def send_dm(
        self,
        recipient_user_id: str,
        message: str,
        comment_id: str,
        rule_id: str,
        max_attempts: int = 3
    ) -> Dict[str, Any]:
        """
        Sends DM via POST /v1/dm/send with retry logic (500 exponential backoff, 429 Retry-After, 400 no retry).
        Uses a stable Idempotency-Key based on rule_id + recipient_user_id.
        """
        idempotency_key = f"{rule_id}:{recipient_user_id}"
        url = f"{self.base_url}/v1/dm/send"
        headers = self._get_headers(idempotency_key=idempotency_key)
        payload = {
            "recipient_user_id": recipient_user_id,
            "message": message,
            "comment_id": comment_id
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    
                    if response.status_code in (200, 202):
                        data = response.json()
                        dm_id = data.get("dm_id")
                        logger.info(f"DM send accepted (HTTP {response.status_code}). dm_id={dm_id}, attempt={attempt}")
                        return {"success": True, "dm_id": dm_id, "attempts": attempt}
                    
                    elif response.status_code == 400:
                        # 400 Bad Request: DO NOT RETRY
                        logger.error(f"DM send 400 Bad Request: {response.text}")
                        return {"success": False, "error": "400 Bad Request", "attempts": attempt}
                    
                    elif response.status_code == 429:
                        # 429 Too Many Requests: Respect Retry-After header
                        retry_after_str = response.headers.get("Retry-After", "1")
                        try:
                            retry_after = float(retry_after_str)
                        except ValueError:
                            retry_after = 1.0
                        
                        logger.warning(f"DM send rate limited (429). Retrying after {retry_after}s (attempt {attempt}/{max_attempts})")
                        if attempt < max_attempts:
                            await asyncio.sleep(retry_after)
                            continue
                        else:
                            return {"success": False, "error": "429 Rate Limit Exceeded", "attempts": attempt}
                    
                    elif response.status_code == 500:
                        # 500 Server Error: Exponential backoff
                        backoff = 1.0 * (2 ** (attempt - 1))  # 1s, 2s, 4s
                        logger.warning(f"DM send 500 Server Error. Retrying after {backoff}s (attempt {attempt}/{max_attempts})")
                        if attempt < max_attempts:
                            await asyncio.sleep(backoff)
                            continue
                        else:
                            return {"success": False, "error": "500 Server Error", "attempts": attempt}
                    
                    else:
                        logger.error(f"Unexpected status code {response.status_code}: {response.text}")
                        if attempt < max_attempts:
                            await asyncio.sleep(1.0)
                            continue
                        return {"success": False, "error": f"HTTP {response.status_code}", "attempts": attempt}
                        
                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    backoff = 1.0 * (2 ** (attempt - 1))
                    logger.warning(f"Network error sending DM: {exc}. Retrying after {backoff}s (attempt {attempt}/{max_attempts})")
                    if attempt < max_attempts:
                        await asyncio.sleep(backoff)
                        continue
                    return {"success": False, "error": str(exc), "attempts": attempt}

        return {"success": False, "error": "Max attempts reached", "attempts": max_attempts}

    async def poll_dm_status(
        self,
        dm_id: str,
        max_polls: int = 5,
        poll_interval: float = 0.5
    ) -> str:
        """
        Poll GET /v1/dm/{dm_id} to reconcile status.
        Possible statuses: 'queued', 'delivered', 'failed'.
        Only returns 'delivered' when confirmed delivered by mock API.
        """
        url = f"{self.base_url}/v1/dm/{dm_id}"
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            for _ in range(max_polls):
                try:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        status = data.get("status", "queued")
                        if status in ("delivered", "failed"):
                            return status
                    await asyncio.sleep(poll_interval)
                except httpx.RequestError as exc:
                    logger.warning(f"Error polling DM status for {dm_id}: {exc}")
                    await asyncio.sleep(poll_interval)

        return "queued"  # Default if polling timed out before final status
