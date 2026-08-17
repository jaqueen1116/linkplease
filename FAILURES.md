# Real-World Failure Modes & Limitations (FAILURES.md)

This document provides an honest technical assessment of real failure modes observed during empirical testing and high-load stress testing (500-event simulation) of this backend implementation.

---

### 1. Downstream Processing Bottlenecks & Worker Throttling Under 500-Event Spike Load

* **Observed Behavior**:
  During the 500-event public webhook stress test (500 webhooks dispatched over 8.7 seconds), `POST /webhook` performed flawlessly, accepting 100% of requests with `HTTP 200 OK` within an average response time of ~497ms. However, as the `asyncio.Queue` worker processed matched deliveries asynchronously, outbound HTTP API calls (`POST /v1/dm/send` and status polling `GET /v1/dm/{dm_id}`) ran into downstream IO bottlenecks on single-core / free-tier host environments (0.1 CPU allocation).
* **Why It Occurs**:
  While webhook ingestion and database persistence are ultra-fast, calling external third-party HTTP endpoints and polling status for hundreds of deliveries concurrently creates network socket and CPU queuing.
* **Mitigation / Next Steps**:
  Use worker thread pools or an async worker pool with bounded concurrency limits (`asyncio.Semaphore(20)`), paired with a persistent task queue (e.g. ARQ or Celery backed by Redis/PostgreSQL).

---

### 2. In-Memory Queue Event Loss on Abrupt Process Termination (`SIGKILL`)

* **Observed Behavior**:
  If the host process experiences an unexpected hard termination (`kill -9`, Out-Of-Memory termination, or host server reboot) while events are queued in memory, in-flight processing items that have not yet created a `deliveries` database row will be lost.
* **Why It Occurs**:
  The application utilizes Python's in-memory `asyncio.Queue`. Although raw webhook payloads are saved to the SQLite `events` table upon HTTP request receipt, items in `asyncio.Queue` reside in RAM.
* **Mitigation / Next Steps**:
  Implement an event recovery scanner that queries the SQLite `events` table on application startup to identify any raw events missing corresponding `deliveries` or `blocked_duplicates` entries and re-enqueues them.

---

### 3. SQLite Single-Writer Lock Contention Under High Parallel Write Concurrency

* **Observed Behavior**:
  Under heavy concurrent bursts (e.g., hundreds of simultaneous comment webhooks arriving within milliseconds), database write transactions can experience connection queuing delay.
* **Why It Occurs**:
  Although SQLite WAL (Write-Ahead Logging) mode allows unlimited concurrent readers alongside a writer, SQLite only permits **one active writer connection** at a time. High write frequency causes connection timeout queuing (`busy_timeout`).
* **Mitigation / Next Steps**:
  For high-scale production deployments (10k+ req/sec), replace SQLite with PostgreSQL or MySQL, which support full multi-version concurrency control (MVCC) and row-level locking.

---

### 4. Status Reconciliation Interruption During Network Disconnection

* **Observed Behavior**:
  If `POST /v1/dm/send` succeeds (returning HTTP 200/202) and saves `dm_id`, but network sockets disconnect or time out during `poll_dm_status()`, the delivery state in SQLite remains in `pending_poll` without reaching a final status (`delivered` or `failed`).
* **Why It Occurs**:
  Status reconciliation relies on an active outbound HTTP polling loop during `process_delivery_job()`. If socket connections drop, polling terminates prematurely.
* **Mitigation / Next Steps**:
  Implement a scheduled background reconciliation cron job that queries all deliveries in SQLite with non-final status (e.g., `pending_poll`) every 5 minutes and re-checks their status at PseudoGram API until a final state is reached.

---

### 5. Inherent API Race Condition on `comment.deleted` Webhooks

* **Observed Behavior**:
  If a user posts a comment and immediately deletes it, but the `comment.deleted` webhook arrives a fraction of a millisecond **after** `POST /v1/dm/send` has already been dispatched to PseudoGram API, the direct message will still be delivered.
* **Why It Occurs**:
  Once an external social network API accepts a DM request, direct messages cannot be recalled or revoked.
* **Mitigation / Next Steps**:
  Introduce a small configurable queue delay (e.g., 2–5 seconds) before executing `POST /v1/dm/send` to give short-interval deletion webhooks an opportunity to cancel pending deliveries.
