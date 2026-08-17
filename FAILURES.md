# Real-World Failure Modes & Limitations (FAILURES.md)

This document provides an honest technical assessment of real failure modes that can occur in this backend implementation under production edge cases.

---

### 1. In-Memory Queue Event Loss on Abrupt Process Crash (`SIGKILL`)

* **Failure Mode**:
  If the application process is abruptly terminated (e.g. `kill -9`, kernel Out-Of-Memory killer, or sudden host server power loss) while an event has been popped from `asyncio.Queue` but before `create_delivery()` is executed in SQLite, that event's processing will be lost.
* **Why It Occurs**:
  While raw webhook payloads are saved to the `events` table in SQLite immediately upon HTTP request receipt, the in-flight processing item in `asyncio.Queue` resides in application RAM.
* **Mitigation / Next Steps**:
  Implement a transactional outbox scanner that periodically polls the `events` table for any events created in the last $N$ minutes that have no corresponding record in the `deliveries` or `blocked_duplicates` tables.

---

### 2. SQLite Write-Lock Contention Under Extreme Spike Concurrency

* **Failure Mode**:
  Under heavy concurrent bursts (e.g., thousands of simultaneous comment webhooks arriving within the same second), requests or worker database writes may raise `sqlite3.OperationalError: database is locked`.
* **Why It Occurs**:
  Although SQLite WAL (Write-Ahead Logging) mode allows unlimited concurrent readers alongside a writer, SQLite only permits **one active writer connection** at a time. High write density causes connection timeout queuing (`busy_timeout`).
* **Mitigation / Next Steps**:
  For high-scale production (10k+ req/sec), replace SQLite with PostgreSQL or MySQL, which support full multi-version concurrency control (MVCC) and row-level locking.

---

### 3. API Secret Key Rotation Mismatch

* **Failure Mode**:
  If the `API_KEY` is rotated or updated on the PseudoGram API platform without updating the backend environment variable and restarting the process, all subsequent incoming webhooks will fail HMAC signature verification with `HTTP 401 Unauthorized`.
* **Why It Occurs**:
  `API_KEY` is loaded from `.env` / environment variables during application initialization in `app/config.py`.
* **Mitigation / Next Steps**:
  Implement secret key rotation support by allowing a key ring (list of active and previous valid secrets) or fetching secrets dynamically from a key management service (e.g., AWS Secrets Manager / Vault).

---

### 4. Polling Reconciliation Interruption During Network Outages

* **Failure Mode**:
  If `POST /v1/dm/send` returns `HTTP 202 Accepted` and saves `dm_id`, but an outbound network disconnect occurs while polling `GET /v1/dm/{dm_id}`, the status may remain unconfirmed or fall back without reaching `delivered`.
* **Why It Occurs**:
  Status reconciliation relies on an active HTTP connection during `poll_dm_status()`. If network sockets time out or drop repeatedly, the polling loop exits.
* **Mitigation / Next Steps**:
  Implement a periodic background reconciliation job that queries all deliveries in SQLite with non-final status (e.g., `pending_poll`) every 5 minutes and re-checks their status at PseudoGram API until a final state (`delivered` or `failed`) is reached.

---

### 5. Inherent Race Condition on `comment.deleted` Events

* **Failure Mode**:
  If a user posts a comment and immediately deletes it, but the `comment.deleted` webhook arrives a fraction of a millisecond **after** `POST /v1/dm/send` has already executed, the direct message will still be delivered to the user.
* **Why It Occurs**:
  Once an external social network API accepts a DM request, direct messages cannot be recalled or revoked.
* **Mitigation / Next Steps**:
  Introduce a small configurable queue delay (e.g., 2–5 seconds) before executing `POST /v1/dm/send` to give short-interval deletion webhooks an opportunity to cancel pending deliveries.
