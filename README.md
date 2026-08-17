# LinkPlease Tech Intern Assignment Backend

A production-minded, clean, and robust Python FastAPI backend designed for automated Instagram comment DM responses with strict deduplication, signature verification, background queue processing, and retry reconciliation.

---

## 🌟 Key Features

1. **HMAC-SHA256 Webhook Verification**: Ensures all incoming webhook requests originated from PseudoGram using constant-time `hmac.compare_digest`.
2. **Database-Enforced Deduplication**: `UNIQUE(rule_id, user_id)` constraint in SQLite ensures a user **never** receives duplicate DMs for the same rule, even under race conditions or duplicate webhooks.
3. **Non-Blocking Webhook Processing**: Webhooks return `HTTP 200` in milliseconds by persisting events to SQLite and offloading processing to an `asyncio.Queue` background worker.
4. **Resilient Retry & Reconciliation Mechanism**:
   - **500 Server Errors**: Exponential backoff (1s, 2s, 4s).
   - **429 Rate Limits**: Honors `Retry-After` HTTP headers.
   - **400 Bad Requests**: Non-retryable failure.
   - **202 Polling Reconciliation**: Polls `GET /v1/dm/{dm_id}` until confirmed `delivered` before marking delivery complete.
5. **Startup Disaster Recovery**: Automatically recovers unprocessed queued deliveries from SQLite upon application restart.
6. **Persistent Statistics**: `/stats` returns accurate counts calculated directly from persistent database records.

---

## 🛠️ Tech Stack

- **Language**: Python 3.10+
- **Framework**: FastAPI + Uvicorn
- **Database**: SQLite3 (with WAL mode enabled)
- **HTTP Client**: HTTPX (async client with timeouts)
- **Concurrency**: `asyncio` (`asyncio.Queue` & background tasks)
- **Testing**: pytest & pytest-asyncio

---

## ⚙️ Environment Variables

Create a `.env` file in the root directory (refer to `.env.example`):

```env
API_KEY=your_pseudogram_api_key_here
DATABASE_PATH=linkplease.db
PSEUDOGRAM_BASE_URL=https://pseudogram-api.onrender.com
PORT=8000
```

| Variable | Description | Default |
| :--- | :--- | :--- |
| `API_KEY` | Secret key used for HMAC signature verification and PseudoGram API calls | `default_secret_api_key` |
| `DATABASE_PATH` | File path for SQLite database | `linkplease.db` |
| `PSEUDOGRAM_BASE_URL` | Base URL for PseudoGram Mock API | `https://pseudogram-api.onrender.com` |
| `PORT` | Port number for Uvicorn server | `8000` |

---

## 🚀 How to Run Locally

### 1. Installation
Clone the repository and install dependencies:

```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Start the Application
Run the FastAPI development server:

```bash
uvicorn app.main:app --reload --port 8000
```

The interactive API documentation (Swagger UI) will be available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

---

## 📡 API Endpoints

### 1. Create Rule
`POST /rules`

**Request Body:**
```json
{
  "keyword": "PRICE",
  "dm_message": "Here is the pricing guide: https://example.com/pricing"
}
```

**Response (HTTP 201 Created):**
```json
{
  "rule_id": "3a8b417e-97c1-4b11-a83a-df2a16d8a391",
  "keyword": "PRICE",
  "dm_message": "Here is the pricing guide: https://example.com/pricing"
}
```

---

### 2. Receive Webhook
`POST /webhook`

**Headers Required:**
```http
X-PseudoGram-Signature: <HMAC-SHA256-hex-signature>
Content-Type: application/json
```

**Payload Example (`comment.created`):**
```json
{
  "event_id": "evt_987654",
  "event_type": "comment.created",
  "data": {
    "comment_id": "cmt_12345",
    "user_id": "usr_999",
    "text": "What is the price of this product?"
  }
}
```

**Response (HTTP 200 OK):**
```json
{
  "status": "ok",
  "event_id": "evt_987654"
}
```

---

### 3. Application Statistics
`GET /stats`

**Response (HTTP 200 OK):**
```json
{
  "sent": 12,
  "failed": 0,
  "queued": 1,
  "duplicates_blocked": 4
}
```

---

## 🔒 How Duplicate Prevention Works

Duplicate prevention is guaranteed at two critical levels:

1. **Webhook Event Deduplication**:
   - `events` table uses `event_id TEXT PRIMARY KEY`.
   - When a webhook arrives, `save_event()` attempts insertion. If the `event_id` already exists, SQLite throws an `IntegrityError`. The endpoint records the event in `blocked_duplicates` and immediately returns `HTTP 200 OK` without re-enqueuing.

2. **User-Rule DM Deduplication**:
   - `deliveries` table enforces `CONSTRAINT unique_rule_user UNIQUE(rule_id, user_id)`.
   - When a comment matches a rule, `create_delivery()` executes an atomic `INSERT`. If the same user has already matched that rule (regardless of comment count or event arrival order), SQLite rejects the transaction with `IntegrityError`.
   - The blocked attempt is recorded in `blocked_duplicates` and no DM API call is made.

---

## 🔁 How Retries & Reconciliation Work

1. **Stable Idempotency Key**:
   - Every DM send request includes `Idempotency-Key: {rule_id}:{recipient_user_id}` to prevent duplicate downstream DM sends at the PseudoGram API.

2. **HTTP Status Handling**:
   - **202 Accepted**: DM is queued at PseudoGram. Saves returned `dm_id` and starts polling `GET /v1/dm/{dm_id}`.
   - **429 Rate Limit**: Parses `Retry-After` header and sleeps before retrying (max 3 attempts).
   - **500 Internal Error**: Uses exponential backoff ($1\text{s}, 2\text{s}, 4\text{s}$) for up to 3 attempts.
   - **400 Bad Request**: Immediately marks delivery as `failed` without retrying.

3. **Status Reconciliation**:
   - Polls `GET /v1/dm/{dm_id}` until the status resolves to `delivered` or `failed`.
   - Delivery is only counted as `sent` when the status is explicitly reported as `delivered`.

---

## 🧪 How to Run Tests

Run all unit and integration tests using `pytest`:

```bash
pytest tests/test_api.py -v
```

The test suite covers:
- Rule creation (`POST /rules`)
- Case-insensitive keyword matching anywhere in comments
- Webhook signature validation & invalid signature rejection
- Event deduplication & user-rule deduplication
- HTTP 500 exponential backoff & HTTP 429 `Retry-After` handling
- DM status polling reconciliation (202 $\rightarrow$ delivered / failed)
- Persistent `/stats` verification

---

## ☁️ Deployment Instructions

1. Set environment variables (`API_KEY`, `PSEUDOGRAM_BASE_URL`, `DATABASE_PATH`).
2. Run via production process manager (e.g. Uvicorn / Gunicorn with uvicorn workers):
   ```bash
   gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app
   ```
3. Ensure the SQLite database file path is mapped to a persistent volume (e.g. Docker Volume or Render Disk) so data persists across container redeployments.
