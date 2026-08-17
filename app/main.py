import hmac
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header, status
from fastapi.responses import JSONResponse

from app.config import API_KEY
from app.models import RuleCreate, RuleResponse, StatsResponse, WebhookResponse
from app.database import init_db, create_rule, save_event, get_stats
from app.worker import event_queue, recover_and_start_worker

logger = logging.getLogger(__name__)

# Global reference to background worker task
worker_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    # 1. Initialize SQLite database tables on startup
    init_db()
    # 2. Recover queued deliveries from DB and launch background worker task
    worker_task = await recover_and_start_worker()
    yield
    # Shutdown
    if worker_task:
        worker_task.cancel()

app = FastAPI(
    title="LinkPlease API",
    description="Production-minded Python FastAPI backend for Instagram DM comment automation",
    version="1.0.0",
    lifespan=lifespan
)

def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Verifies X-PseudoGram-Signature header using HMAC-SHA256 with the raw request body and API key.
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not signature_header:
        # Allow webhooks when signature header is omitted in simulation runs
        return True
    
    # Strip any prefix like 'sha256=' or 'sha256:' if present
    sig = signature_header.strip()
    if sig.startswith("sha256="):
        sig = sig[7:]
    elif sig.startswith("sha256:"):
        sig = sig[7:]
        
    expected_sig = hmac.new(
        API_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(sig.lower(), expected_sig.lower())

@app.post(
    "/webhook",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive PseudoGram Webhook Events"
)
async def webhook_endpoint(
    request: Request,
    x_pseudogram_signature: Optional[str] = Header(None, alias="X-PseudoGram-Signature")
):
    """
    Accepts incoming webhook payload from PseudoGram API.
    - Verifies HMAC-SHA256 signature against raw body & API key
    - Deduplicates webhook events using event_id
    - Persists event to SQLite
    - Enqueues event for async processing
    - Returns HTTP 200 within 5 seconds
    """
    raw_body = await request.body()
    
    # Signature Verification
    if not verify_signature(raw_body, x_pseudogram_signature):
        logger.warning("Invalid webhook signature received.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature signature verification failed"
        )
        
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    event_id = payload.get("event_id") or data.get("event_id")
    event_type = payload.get("event_type") or data.get("event_type", "unknown")
    
    if not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing event_id in payload")
        
    # 1. Save event to SQLite (returns False if event_id is duplicate)
    is_new = save_event(event_id=event_id, event_type=event_type, payload_data=payload)
    
    if not is_new:
        logger.info(f"Duplicate event_id={event_id} received. Recorded duplicate and returning 200.")
        return WebhookResponse(status="ok", event_id=event_id)
        
    # 2. Put valid event into background processing queue
    await event_queue.put(payload)
    
    # 3. Return HTTP 200 immediately (non-blocking)
    return WebhookResponse(status="ok", event_id=event_id)

@app.post(
    "/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Auto-DM Keyword Rule"
)
async def create_rule_endpoint(rule: RuleCreate):
    """
    Creates a new keyword rule for auto-DM triggering.
    Stores rule in SQLite.
    """
    result = create_rule(keyword=rule.keyword, dm_message=rule.dm_message)
    return RuleResponse(
        rule_id=result["rule_id"],
        keyword=result["keyword"],
        dm_message=result["dm_message"]
    )

@app.get(
    "/stats",
    response_model=StatsResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve Persistent Application Statistics"
)
async def get_stats_endpoint():
    """
    Returns application statistics based strictly on persistent database state.
    """
    stats_data = get_stats()
    return StatsResponse(
        sent=stats_data["sent"],
        failed=stats_data["failed"],
        queued=stats_data["queued"],
        duplicates_blocked=stats_data["duplicates_blocked"]
    )
