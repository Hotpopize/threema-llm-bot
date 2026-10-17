# Threema Gateway LLM Bot (v1)

A production-ready, asynchronous Python bot that integrates [Threema Gateway](https://gateway.threema.ch/) with modern LLMs (OpenAI or local Ollama) using the official `threema.gateway` SDK, `aiohttp`, and `ngrok`.

---

## Architecture & Lifecycle

```
[ Threema User ]
       │  (1) E2E Encrypted Message
       ▼
[ Threema Gateway Server ]
       │  (2) HTTP POST Webhook
       ▼
[ ngrok Public Tunnel ]
       │  (3) Local Forward
       ▼
[ aiohttp Webhook Server ]
       │  (4) Fast-fail structural checks
       │  (5) HMAC-SHA256 signature validation
       │  (6) NaCl E2E Decryption (cached public key)
       ▼
[ Async LLM Engine ] ──▶ Concurrency Semaphore (max 4)
       │                 Drafts response via OpenAI or Ollama
       ▼
[ Threema E2E Sender ]
       │  (7) Encrypts with user's cached public key
       │  (8) POST to https://msgapi.threema.ch/send_e2e
       ▼
[ Audit Trail ]
       │  (9) Appends structured JSON to interactions.jsonl
       │ (10) Logs execution to bot.log
       ▼
[ Delivery Receipts Filter ]
       - Drops subsequent 'received' & 'read' receipts to prevent loops
```

---

## Built-In Optimizations

1. **Async Public Key Caching:** Caches public key lookups in memory to eliminate redundant HTTP GET `/pubkeys/{id}` roundtrips.
2. **Async Task Retention & Drain:** Keeps strong references to background LLM tasks (`bg_tasks`) to avoid garbage collection mid-await and gracefully drains them on `Ctrl+C`.
3. **Webhook Deduplication Buffer:** In-memory ring buffer (`deque(maxlen=512)`) drops duplicate webhooks from network retries before expensive HMAC/decryption/LLM operations.
4. **Fast-Fail Crypto Checks:** Validates $O(1)$ structural constraints (`to_id`, length of `from_id`) prior to HMAC and NaCl operations.
5. **Persistent Audit Stream:** Uses a continuous file handle with explicit flushing rather than opening/closing `interactions.jsonl` on every webhook hit.
6. **Delivery Receipt Shield:** Filters out `DeliveryReceipt` events so read/delivery confirmations never trigger bot reply loops.
7. **Cross-Platform Libsodium Support:** Includes automatic `os.add_dll_directory` loading for `libsodium.dll` on Windows.

---

## Prerequisites

- **Python:** 3.10 or higher.
- **Threema Gateway ID:** Must be in **End-to-End (E2E)** mode (starts with `*`, e.g., `*YOUR_ID`). Basic mode is not supported for receiving.
- **LLM Provider:** An OpenAI API key or a local instance of [Ollama](https://ollama.com/).
- **ngrok:** For exposing your local port to the internet during development.

---

## Quickstart Guide

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/Hotpopize/threema-llm-bot.git
cd threema-llm-bot

# Create and activate virtual environment
python -m venv .venv

# Windows:
.\.venv\Scripts\activate

# macOS / Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

> **Windows Users:** `libsodium.dll` is already included in the root directory. Python will load it automatically.
> **Linux / macOS Users:** Ensure libsodium is installed on your OS:
> - macOS: `brew install libsodium`
> - Ubuntu / Debian: `sudo apt-get install -y libsodium-dev`

### 2. Configure Environment (`.env`)

Copy the template and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:
```ini
THREEMA_GATEWAY_ID=*YOUR_ID
THREEMA_GATEWAY_SECRET=your_gateway_secret
THREEMA_PRIVATE_KEY=private:0123456789abcdef...

# Choose your LLM:
OPENAI_API_KEY=sk-proj-your_key_here
OPENAI_MODEL=gpt-4o-mini

# Or use local Ollama:
# LLM_BASE_URL=http://127.0.0.1:11434/v1
# LLM_MODEL=gemma3:4b

PORT=8080
```

### 3. Run the Test Suite

Verify all components, validation filters, deduplication, and mock sends:

```bash
python test_bot.py
```

### 4. Start the Bot & Expose Webhook

**Terminal 1 — Run the bot:**
```bash
python bot.py
```

**Terminal 2 — Start ngrok tunnel:**
```bash
ngrok http 8080
# Or with a static domain:
# ngrok http 8080 --url https://your-domain.ngrok-free.dev
```

### 5. Set Callback in Threema Gateway Console

1. Log in to [Threema Gateway Console](https://gateway.threema.ch/).
2. Select your ID → **Edit**.
3. Set **URL for incoming messages** to:
   ```plaintext
   https://<your-ngrok-subdomain>.ngrok-free.dev/webhook/threema
   ```
4. Click **Save**.

---

## Audit Logs & Monitoring

- **`bot.log`**: Human-readable server logs with step-by-step progress for each webhook hit.
- **`interactions.jsonl`**: Machine-readable JSONL audit log containing:
  ```json
  {
    "status": "success",
    "sender_id": "NSW942PU",
    "gateway_id": "*IAMABOT",
    "incoming_message_id": "1a0a04923a96412d",
    "incoming_text": "Are u free for a drink next Friday",
    "llm_model": "gemma3:4b",
    "drafted_reply": "Thank you for the message! They will reply personally later. What is it about?",
    "outgoing_message_id": "f12165c63ce33a55",
    "error": null,
    "timestamp_utc": "2026-10-17T20:01:03.416549+00:00"
  }
  ```

---

## Collaborators & Contributing

1. Fork or clone this repository.
2. Never commit `.env` or sensitive private keys (enforced by `.gitignore`).
3. Run `python test_bot.py` before submitting pull requests.
