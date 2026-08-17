import os
import hmac
import hashlib
import json
import pytest
import asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.config import API_KEY
import app.database as db
import app.worker as worker
from app.pseudogram import PseudoGramClient

TEST_DB_PATH = "test_linkplease.db"

def make_signature(body_bytes: bytes, secret: str = API_KEY) -> str:
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """
    Fixture to isolate database state for each test.
    """
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        
    monkeypatch.setattr("app.database.DATABASE_PATH", TEST_DB_PATH)
    monkeypatch.setattr("app.main.API_KEY", API_KEY)
    
    db.init_db(db_path=TEST_DB_PATH)
    yield
    
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

# 1. Test Creating a Rule
def test_create_rule(setup_test_db):
    client = TestClient(app)
    response = client.post("/rules", json={
        "keyword": "PRICE",
        "dm_message": "Here is our pricing list!"
    })
    assert response.status_code == 201
    data = response.json()
    assert "rule_id" in data
    assert data["keyword"] == "PRICE"
    assert data["dm_message"] == "Here is our pricing list!"

# 2 & 3. Test Keyword Matching (Case-insensitive & anywhere in comment text)
@pytest.mark.asyncio
async def test_keyword_matching(setup_test_db):
    rule = db.create_rule("PRICE", "Here is the pricing", db_path=TEST_DB_PATH)
    
    # Matching comment
    payload = {
        "event_id": "evt_match_1",
        "event_type": "comment.created",
        "data": {
            "comment_id": "cmt_1",
            "user_id": "usr_1",
            "text": "Hey there! Can you send me the price details please?"
        }
    }
    
    # Mock PseudoGram client to track sent DMs
    sent_dms = []
    class MockClient(PseudoGramClient):
        async def send_dm(self, recipient_user_id, message, comment_id, rule_id, max_attempts=3):
            sent_dms.append({"user_id": recipient_user_id, "message": message})
            return {"success": True, "dm_id": "dm_mock_1"}
        async def poll_dm_status(self, dm_id, max_polls=5, poll_interval=0.1):
            return "delivered"

    mock_client = MockClient()
    await worker.process_event(payload, client=mock_client, db_path=TEST_DB_PATH)
    
    assert len(sent_dms) == 1
    assert sent_dms[0]["user_id"] == "usr_1"
    
    stats = db.get_stats(db_path=TEST_DB_PATH)
    assert stats["sent"] == 1

# 4. Test Duplicate event_id
def test_duplicate_event_id(setup_test_db):
    client = TestClient(app)
    payload = {
        "event_id": "evt_dup_100",
        "event_type": "comment.created",
        "data": {
            "comment_id": "cmt_100",
            "user_id": "usr_100",
            "text": "Hello world"
        }
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = make_signature(body_bytes)
    headers = {"X-PseudoGram-Signature": sig, "Content-Type": "application/json"}
    
    # First webhook post -> HTTP 200
    res1 = client.post("/webhook", content=body_bytes, headers=headers)
    assert res1.status_code == 200
    
    # Second webhook post with SAME event_id -> HTTP 200 & duplicate recorded
    res2 = client.post("/webhook", content=body_bytes, headers=headers)
    assert res2.status_code == 200
    
    stats = client.get("/stats").json()
    assert stats["duplicates_blocked"] >= 1

# 5. Test Same User Commenting Multiple Times (Enforces UNIQUE(rule_id, user_id))
@pytest.mark.asyncio
async def test_same_user_multiple_comments(setup_test_db):
    rule = db.create_rule("DISCOUNT", "Here is your discount code!", db_path=TEST_DB_PATH)
    
    payload1 = {
        "event_id": "evt_user1_c1",
        "event_type": "comment.created",
        "data": {"comment_id": "cmt_1", "user_id": "usr_repeat", "text": "Give me DISCOUNT!"}
    }
    payload2 = {
        "event_id": "evt_user1_c2",
        "event_type": "comment.created",
        "data": {"comment_id": "cmt_2", "user_id": "usr_repeat", "text": "I want DISCOUNT again!"}
    }
    
    sent_count = 0
    class MockClient(PseudoGramClient):
        async def send_dm(self, recipient_user_id, message, comment_id, rule_id, max_attempts=3):
            nonlocal sent_count
            sent_count += 1
            return {"success": True, "dm_id": f"dm_{sent_count}"}
        async def poll_dm_status(self, dm_id, max_polls=5, poll_interval=0.1):
            return "delivered"

    mock_client = MockClient()
    await worker.process_event(payload1, client=mock_client, db_path=TEST_DB_PATH)
    await worker.process_event(payload2, client=mock_client, db_path=TEST_DB_PATH)
    
    # Only 1 DM sent to usr_repeat despite 2 comments matching rule!
    assert sent_count == 1
    stats = db.get_stats(db_path=TEST_DB_PATH)
    assert stats["sent"] == 1
    assert stats["duplicates_blocked"] >= 1

# 6. Test Different Users Receiving DMs
@pytest.mark.asyncio
async def test_different_users_receive_dms(setup_test_db):
    rule = db.create_rule("LINK", "Here is your link!", db_path=TEST_DB_PATH)
    
    p1 = {
        "event_id": "e1",
        "event_type": "comment.created",
        "data": {"comment_id": "c1", "user_id": "user_A", "text": "Send me the LINK"}
    }
    p2 = {
        "event_id": "e2",
        "event_type": "comment.created",
        "data": {"comment_id": "c2", "user_id": "user_B", "text": "Can I get the LINK too?"}
    }
    
    sent_users = []
    class MockClient(PseudoGramClient):
        async def send_dm(self, recipient_user_id, message, comment_id, rule_id, max_attempts=3):
            sent_users.append(recipient_user_id)
            return {"success": True, "dm_id": f"dm_{recipient_user_id}"}
        async def poll_dm_status(self, dm_id, max_polls=5, poll_interval=0.1):
            return "delivered"

    mock_client = MockClient()
    await worker.process_event(p1, client=mock_client, db_path=TEST_DB_PATH)
    await worker.process_event(p2, client=mock_client, db_path=TEST_DB_PATH)
    
    assert set(sent_users) == {"user_A", "user_B"}
    stats = db.get_stats(db_path=TEST_DB_PATH)
    assert stats["sent"] == 2

# 7. Test Invalid Webhook Signature
def test_invalid_webhook_signature(setup_test_db):
    client = TestClient(app)
    payload = {"event_id": "evt_bad_sig", "event_type": "comment.created", "data": {"text": "hello"}}
    body_bytes = json.dumps(payload).encode("utf-8")
    
    headers = {"X-PseudoGram-Signature": "invalid_signature_hash", "Content-Type": "application/json"}
    res = client.post("/webhook", content=body_bytes, headers=headers)
    assert res.status_code == 401

# 8. Test Mock API 500 Retry
@pytest.mark.asyncio
async def test_mock_api_500_retry(monkeypatch):
    client = PseudoGramClient(base_url="https://fake-mock-api.com", api_key="test_key")
    attempts_made = 0

    class MockResponse:
        def __init__(self, status_code):
            self.status_code = status_code
        def json(self):
            return {"dm_id": "dm_500_fixed"}

    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def post(self, url, json, headers):
            nonlocal attempts_made
            attempts_made += 1
            if attempts_made == 1:
                return MockResponse(500)
            return MockResponse(202)

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: MockAsyncClient())
    
    # We patch sleep to prevent waiting during test execution
    async def fast_sleep(sec): pass
    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    
    res = await client.send_dm("u1", "msg", "c1", "r1", max_attempts=3)
    assert res["success"] is True
    assert attempts_made == 2

# 9. Test Mock API 429 Retry
@pytest.mark.asyncio
async def test_mock_api_429_retry(monkeypatch):
    client = PseudoGramClient(base_url="https://fake-mock-api.com", api_key="test_key")
    attempts_made = 0

    class MockResponse429:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
        def json(self):
            return {"dm_id": "dm_429_fixed"}

    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def post(self, url, json, headers):
            nonlocal attempts_made
            attempts_made += 1
            if attempts_made == 1:
                return MockResponse429(429, headers={"Retry-After": "0.01"})
            return MockResponse429(202)

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: MockAsyncClient())
    
    async def fast_sleep(sec): pass
    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    
    res = await client.send_dm("u1", "msg", "c1", "r1", max_attempts=3)
    assert res["success"] is True
    assert attempts_made == 2

# 10 & 11. Test 202 Followed by Delivered vs Failed
@pytest.mark.asyncio
async def test_202_followed_by_delivered_and_failed(setup_test_db):
    rule = db.create_rule("INFO", "Here is info", db_path=TEST_DB_PATH)
    
    # Test 1: Delivered
    d1 = db.create_delivery(rule["rule_id"], "usr_deliv", "cmt_deliv", db_path=TEST_DB_PATH)
    class DeliveredClient(PseudoGramClient):
        async def send_dm(self, recipient_user_id, message, comment_id, rule_id, max_attempts=3):
            return {"success": True, "dm_id": "dm_ok"}
        async def poll_dm_status(self, dm_id, max_polls=5, poll_interval=0.1):
            return "delivered"

    await worker.process_delivery_job(
        delivery_id=d1["id"],
        rule_id=rule["rule_id"],
        user_id="usr_deliv",
        comment_id="cmt_deliv",
        dm_message="Here is info",
        client=DeliveredClient(),
        db_path=TEST_DB_PATH
    )
    
    # Test 2: Failed
    d2 = db.create_delivery(rule["rule_id"], "usr_fail", "cmt_fail", db_path=TEST_DB_PATH)
    class FailedClient(PseudoGramClient):
        async def send_dm(self, recipient_user_id, message, comment_id, rule_id, max_attempts=3):
            return {"success": True, "dm_id": "dm_fail"}
        async def poll_dm_status(self, dm_id, max_polls=5, poll_interval=0.1):
            return "failed"

    await worker.process_delivery_job(
        delivery_id=d2["id"],
        rule_id=rule["rule_id"],
        user_id="usr_fail",
        comment_id="cmt_fail",
        dm_message="Here is info",
        client=FailedClient(),
        db_path=TEST_DB_PATH
    )

    stats = db.get_stats(db_path=TEST_DB_PATH)
    assert stats["sent"] == 1
    assert stats["failed"] == 1

# 12. Test /stats Endpoint
def test_stats_endpoint(setup_test_db):
    client = TestClient(app)
    res = client.get("/stats")
    assert res.status_code == 200
    data = res.json()
    assert "sent" in data
    assert "failed" in data
    assert "queued" in data
    assert "duplicates_blocked" in data

# 13. Test comment.deleted prevents pending DM from sending
@pytest.mark.asyncio
async def test_comment_deleted_prevents_sending(setup_test_db):
    rule = db.create_rule("LINK", "Here is link", db_path=TEST_DB_PATH)
    
    # Create queued delivery manually
    delivery = db.create_delivery(rule["rule_id"], "usr_del", "cmt_del_123", db_path=TEST_DB_PATH)
    assert delivery["status"] == "queued"
    
    # Process comment.deleted event
    deleted_event = {
        "event_id": "evt_del_99",
        "event_type": "comment.deleted",
        "data": {"comment_id": "cmt_del_123"}
    }
    
    await worker.process_event(deleted_event, db_path=TEST_DB_PATH)
    
    # Verify delivery status changed to 'cancelled'
    with db.get_db(TEST_DB_PATH) as conn:
        row = conn.execute("SELECT status FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()
        assert row["status"] == "cancelled"

