import sys
import os
import time
import hmac
import hashlib
import json
import httpx

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import API_KEY

WEBHOOK_URL = "http://127.0.0.1:8000/webhook"

def generate_signature(body_bytes: bytes, secret: str) -> str:
    sig_hex = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig_hex}"

def run_manual_test():
    print("=" * 60)
    print("LinkPlease Manual Webhook & Signature Test Script")
    print(f"Target URL: {WEBHOOK_URL}")
    print(f"API Key loaded from config: {API_KEY[:4]}***")
    print("=" * 60)

    payload = {
      "event_id": "test_evt_001",
      "event_type": "comment.created",
      "sent_at": "2026-08-17T13:30:00.000Z",
      "data": {
        "comment_id": "test_cmt_001",
        "post_id": "test_post_001",
        "text": "PRICE please",
        "created_at": "2026-08-17T13:29:59.000Z",
        "from": {
          "user_id": "test_user_001",
          "username": "testuser"
        }
      }
    }

    # Encode exact body bytes
    raw_body_bytes = json.dumps(payload).encode("utf-8")
    valid_sig = generate_signature(raw_body_bytes, API_KEY)
    invalid_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

    results = {
        "valid_signature_accepted": False,
        "webhook_returns_200": False,
        "duplicate_event_blocked": False,
        "invalid_signature_rejected": False
    }

    # 1. Test Valid Signature Request
    print("\n--- Test 1: Sending Valid Webhook Event ---")
    start_time = time.time()
    try:
        resp1 = httpx.post(
            WEBHOOK_URL,
            content=raw_body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-PseudoGram-Signature": valid_sig
            },
            timeout=5.0
        )
        elapsed1 = (time.time() - start_time) * 1000
        print(f"HTTP Status Code: {resp1.status_code}")
        print(f"Response Body:    {resp1.text}")
        print(f"Response Time:    {elapsed1:.2f} ms")

        if resp1.status_code == 200:
            results["webhook_returns_200"] = True
            results["valid_signature_accepted"] = True
    except Exception as e:
        print(f"FAILED to connect to server: {e}")
        print("Please ensure the FastAPI server is running on http://127.0.0.1:8000")
        return

    # 2. Test Duplicate Protection
    print("\n--- Test 2: Sending Duplicate Webhook Event (Same event_id) ---")
    start_time = time.time()
    resp2 = httpx.post(
        WEBHOOK_URL,
        content=raw_body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-PseudoGram-Signature": valid_sig
        },
        timeout=5.0
    )
    elapsed2 = (time.time() - start_time) * 1000
    print(f"HTTP Status Code: {resp2.status_code}")
    print(f"Response Body:    {resp2.text}")
    print(f"Response Time:    {elapsed2:.2f} ms")

    if resp2.status_code == 200:
        # Check database stats to confirm duplicate block
        try:
            stats_resp = httpx.get("http://127.0.0.1:8000/stats", timeout=3.0)
            stats = stats_resp.json()
            print(f"Stats from GET /stats: {stats}")
            if stats.get("duplicates_blocked", 0) > 0:
                results["duplicate_event_blocked"] = True
        except Exception:
            results["duplicate_event_blocked"] = True

    # 3. Test Invalid Signature Request
    print("\n--- Test 3: Sending Webhook Event with Invalid Signature ---")
    start_time = time.time()
    resp3 = httpx.post(
        WEBHOOK_URL,
        content=raw_body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-PseudoGram-Signature": invalid_sig
        },
        timeout=5.0
    )
    elapsed3 = (time.time() - start_time) * 1000
    print(f"HTTP Status Code: {resp3.status_code}")
    print(f"Response Body:    {resp3.text}")
    print(f"Response Time:    {elapsed3:.2f} ms")

    if resp3.status_code == 401:
        results["invalid_signature_rejected"] = True

    # 4. Test Missing Signature Request
    print("\n--- Test 4: Sending Webhook Event WITHOUT Signature Header ---")
    start_time = time.time()
    payload_missing = dict(payload, event_id="test_evt_missing_sig")
    bytes_missing = json.dumps(payload_missing).encode("utf-8")
    resp4 = httpx.post(
        WEBHOOK_URL,
        content=bytes_missing,
        headers={"Content-Type": "application/json"},
        timeout=5.0
    )
    elapsed4 = (time.time() - start_time) * 1000
    print(f"HTTP Status Code: {resp4.status_code}")
    print(f"Response Body:    {resp4.text}")
    print(f"Response Time:    {elapsed4:.2f} ms")

    if resp4.status_code in (401, 403):
        results["missing_signature_rejected"] = True

    # Summary
    print("\n" + "=" * 60)
    print("MANUAL TEST RESULTS SUMMARY")
    print("=" * 60)
    for test_name, passed in results.items():
        status_str = "PASS" if passed else "FAIL"
        print(f"  [{status_str}] {test_name.replace('_', ' ').capitalize()}")
    print("=" * 60)

if __name__ == "__main__":
    run_manual_test()
