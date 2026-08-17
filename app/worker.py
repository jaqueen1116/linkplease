import asyncio
import logging
from typing import Optional, Dict, Any
from app.database import (
    get_all_rules,
    create_delivery,
    update_delivery_status,
    cancel_pending_delivery_by_comment,
    get_queued_deliveries,
)
from app.pseudogram import PseudoGramClient

logger = logging.getLogger(__name__)

# Global asyncio queue for background processing of webhook events
event_queue: asyncio.Queue = asyncio.Queue()

async def process_delivery_job(
    delivery_id: str,
    rule_id: str,
    user_id: str,
    comment_id: str,
    dm_message: str,
    client: Optional[PseudoGramClient] = None,
    db_path: Optional[str] = None
):
    """
    Executes DM sending, retries, and status reconciliation for a single delivery job.
    """
    client = client or PseudoGramClient()
    
    # Send DM via PseudoGram client
    res = await client.send_dm(
        recipient_user_id=user_id,
        message=dm_message,
        comment_id=comment_id,
        rule_id=rule_id
    )
    
    if res.get("success") and res.get("dm_id"):
        dm_id = res["dm_id"]
        # Update delivery record with dm_id
        update_delivery_status(delivery_id, status="pending_poll", dm_id=dm_id, increment_attempts=True, db_path=db_path)
        
        # Poll status for delivery reconciliation
        final_status = await client.poll_dm_status(dm_id)
        
        if final_status == "delivered":
            update_delivery_status(delivery_id, status="sent", db_path=db_path)
            logger.info(f"Delivery {delivery_id} successfully sent and delivered to user {user_id}")
        elif final_status == "failed":
            # Initial send accepted but later failed reconciliation -> retry once if attempts permit or mark failed
            logger.warning(f"Delivery {delivery_id} poll reported failed status.")
            update_delivery_status(delivery_id, status="failed", db_path=db_path)
        else:
            # Still queued / pending on mock API -> mark sent if accepted, or keep pending
            update_delivery_status(delivery_id, status="sent", db_path=db_path)
    else:
        # Initial send POST failed after retries
        update_delivery_status(delivery_id, status="failed", increment_attempts=True, db_path=db_path)
        logger.error(f"Delivery {delivery_id} failed: {res.get('error')}")

async def process_event(
    event_data: Dict[str, Any],
    client: Optional[PseudoGramClient] = None,
    db_path: Optional[str] = None
):
    """
    Processes a single event payload from the queue.
    """
    data = event_data.get("data") if isinstance(event_data.get("data"), dict) else event_data
    event_type = event_data.get("event_type") or data.get("event_type")
    
    if event_type == "comment.deleted":
        comment_id = data.get("comment_id")
        if comment_id:
            cancelled = cancel_pending_delivery_by_comment(comment_id, db_path=db_path)
            if cancelled:
                logger.info(f"Cancelled pending delivery for deleted comment_id={comment_id}")
        return

    comment_id = data.get("comment_id")
    user_id = data.get("user_id")
    if not user_id and isinstance(data.get("from"), dict):
        user_id = data.get("from", {}).get("user_id")
    comment_text = data.get("text", "")
    
    if not comment_id or not user_id or not comment_text:
        logger.warning(f"Event missing required fields: comment_id={comment_id}, user_id={user_id}")
        return

    rules = get_all_rules(db_path=db_path)
    for rule in rules:
        keyword = rule["keyword"]
        # Case-insensitive keyword matching anywhere in comment text
        if keyword.lower() in comment_text.lower():
            logger.info(f"Matched rule '{keyword}' for comment '{comment_text}' from user {user_id}")
            
            # Atomic database insertion enforcing UNIQUE(rule_id, user_id)
            delivery = create_delivery(
                rule_id=rule["id"],
                user_id=user_id,
                comment_id=comment_id,
                db_path=db_path
            )
            
            if delivery is None:
                logger.info(f"Duplicate delivery blocked by database UNIQUE constraint for user {user_id} and rule {rule['id']}")
                continue

            # Process delivery job asynchronously
            await process_delivery_job(
                delivery_id=delivery["id"],
                rule_id=rule["id"],
                user_id=user_id,
                comment_id=comment_id,
                dm_message=rule["dm_message"],
                client=client,
                db_path=db_path
            )

async def worker_loop(client: Optional[PseudoGramClient] = None, db_path: Optional[str] = None):
    """
    Continuous worker loop popping events from event_queue.
    """
    logger.info("Background worker loop started.")
    while True:
        try:
            event_data = await event_queue.get()
            await process_event(event_data, client=client, db_path=db_path)
            event_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Background worker loop cancelled.")
            break
        except Exception as exc:
            logger.error(f"Error in background worker loop: {exc}", exc_info=True)

async def recover_and_start_worker(client: Optional[PseudoGramClient] = None, db_path: Optional[str] = None) -> asyncio.Task:
    """
    Recovers unhandled queued deliveries from SQLite on app startup and launches background worker task.
    """
    queued_deliveries = get_queued_deliveries(db_path=db_path)
    if queued_deliveries:
        logger.info(f"Recovering {len(queued_deliveries)} queued deliveries from database on startup...")
        rules_map = {r["id"]: r for r in get_all_rules(db_path=db_path)}
        for delivery in queued_deliveries:
            rule = rules_map.get(delivery["rule_id"])
            if rule:
                asyncio.create_task(
                    process_delivery_job(
                        delivery_id=delivery["id"],
                        rule_id=delivery["rule_id"],
                        user_id=delivery["user_id"],
                        comment_id=delivery["comment_id"],
                        dm_message=rule["dm_message"],
                        client=client,
                        db_path=db_path
                    )
                )
    
    worker_task = asyncio.create_task(worker_loop(client=client, db_path=db_path))
    return worker_task
