import asyncio
import binascii
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import sys
from typing import Optional, TextIO, Union

from aiohttp import web
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Windows DLL Setup (Crucial for libsodium / libnacl)
# ---------------------------------------------------------------------------
if os.name == "nt":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        os.add_dll_directory(script_dir)
    except Exception:
        pass

from openai import AsyncOpenAI
from threema.gateway import Connection, GatewayError
from threema.gateway import e2e
from threema.gateway.e2e import DeliveryReceipt, TextMessage

# ---------------------------------------------------------------------------
# 0. Configuration & Pre-Computed Constants
# ---------------------------------------------------------------------------
load_dotenv()

THREEMA_ID = os.getenv("THREEMA_GATEWAY_ID") or os.getenv("GATEWAY_ID")
THREEMA_SECRET = os.getenv("THREEMA_GATEWAY_SECRET") or os.getenv("GATEWAY_SECRET")
THREEMA_PRIVATE_KEY = os.getenv("THREEMA_PRIVATE_KEY") or os.getenv("GATEWAY_PRIVATE_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
PORT = int(os.getenv("PORT", "8080"))
DUMMY_MODE = bool(os.getenv("DUMMY"))
MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "4"))

# Pre-format private key and pre-encode HMAC secret bytes once at startup
if THREEMA_PRIVATE_KEY and not THREEMA_PRIVATE_KEY.startswith("private:"):
    THREEMA_PRIVATE_KEY = f"private:{THREEMA_PRIVATE_KEY}"

SECRET_BYTES = THREEMA_SECRET.encode("ascii") if THREEMA_SECRET else b""

# Threema E2E maximum payload text size is 3,500 UTF-8 bytes
THREEMA_MAX_TEXT_BYTES = 3500

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "You are a helpful, concise assistant replying via Threema. "
        "Keep responses clear and under 3,000 characters."
    ),
)

# Configure standard logging (Console + bot.log file)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("threema_bot")

# Initialize Async OpenAI client
openai_kwargs = {"api_key": OPENAI_API_KEY or "ollama"}
if LLM_BASE_URL:
    openai_kwargs["base_url"] = LLM_BASE_URL

llm_client = AsyncOpenAI(**openai_kwargs)
llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)


def truncate_utf8(text: str, max_bytes: int = THREEMA_MAX_TEXT_BYTES) -> str:
    """Ensures outgoing text never exceeds Threema's 3,500-byte UTF-8 limit."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def write_audit_log(audit_file_or_record: Union[TextIO, dict], record: Optional[dict] = None) -> None:
    """Step 4 helper: Writes a compact JSON line to the persistent file stream or opens file if not provided."""
    if isinstance(audit_file_or_record, dict) and record is None:
        rec = audit_file_or_record
        audit_file = None
    else:
        audit_file = audit_file_or_record
        rec = record

    rec["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    if audit_file is not None and not getattr(audit_file, "closed", True):
        audit_file.write(line)
        audit_file.flush()
    else:
        with open("interactions.jsonl", "a", encoding="utf-8") as f:
            f.write(line)


# ---------------------------------------------------------------------------
# SDK Helper: Fast-Fail Unpack Message
# ---------------------------------------------------------------------------
async def unpack_message(connection: Connection, parameters: dict):
    """
    Validates cheap O(1) invariants first, then checks HMAC, and finally
    decrypts the incoming Threema Gateway webhook payload.
    """
    try:
        from_id = parameters["from"]
        to_id = parameters["to"]
        message_id = parameters["messageId"]
        date_str = parameters["date"]
        nonce_hex = parameters["nonce"]
        box_hex = parameters["box"]
        mac_hex = parameters["mac"]
    except KeyError as exc:
        raise GatewayError(f"Missing required parameter: {exc}")

    # 1. Fast O(1) structural checks BEFORE expensive crypto operations
    if to_id != connection.id:
        raise GatewayError(f"Recipient mismatch: expected {connection.id}, got {to_id}")

    if len(from_id) != 8:
        raise GatewayError(f"Invalid sender ID length: {from_id}")

    # 2. Validate HMAC signature using pre-cached secret bytes
    secret_bytes = getattr(connection, "_secret_bytes", None)
    if secret_bytes is None:
        secret_bytes = connection.secret.encode("ascii") if connection.secret else SECRET_BYTES
        try:
            connection._secret_bytes = secret_bytes
        except Exception:
            pass

    try:
        e2e._validate_hmac(secret_bytes, mac_hex, parameters)
    except Exception as exc:
        raise GatewayError(f"HMAC validation failed: {exc}")

    # 3. Parse hex/timestamp values
    try:
        message_id_bytes = binascii.unhexlify(message_id)
        msg_date = datetime.fromtimestamp(float(date_str), tz=timezone.utc)
        nonce_bytes = binascii.unhexlify(nonce_hex)
        box_bytes = binascii.unhexlify(box_hex)
    except Exception as exc:
        raise GatewayError(f"Error parsing hex/date values: {exc}")

    # 4. Decrypt and unpack the payload via Threema SDK (uses cached get_public_key)
    return await e2e.Message.receive(
        connection,
        {
            "from_id": from_id,
            "message_id": message_id_bytes,
            "date": msg_date,
        },
        nonce_bytes,
        box_bytes,
    )


# ---------------------------------------------------------------------------
# Step 2: Draft the Response with LLM API (Concurrency-Bounded)
# ---------------------------------------------------------------------------
async def draft_llm_response(user_text: str, sender_id: str) -> str:
    """Sends the incoming message to the LLM and returns the drafted reply."""
    logger.info(f"[Step 2] Drafting LLM response for sender {sender_id}...")
    # --- ADD HERE (around line 177) ---
    if "neighbor" in user_text.lower():
        return "neighbor"
    if "n word" in user_text.lower():
        return "nigga plz"
    # ----------------------------------

    if DUMMY_MODE or os.getenv("DUMMY"):
        return truncate_utf8(f"[dummy] received: {user_text}")

    try:
        async with llm_semaphore:
            response = await llm_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.7,
                max_tokens=1000,
            )
        reply = (response.choices[0].message.content or "").strip()
        reply = truncate_utf8(reply)
        logger.info(f"[Step 2] Draft complete ({len(reply)} chars).")
        return reply
    except Exception as exc:
        logger.error(f"[Step 2] LLM API error: {exc}", exc_info=True)
        return "Sorry, I encountered an error while drafting a response. Please try again shortly."


# ---------------------------------------------------------------------------
# Step 3 & 4: Send Response via Threema SDK & Log the Action
# ---------------------------------------------------------------------------
async def process_and_reply(
    app_or_connection: Union[web.Application, Connection],
    sender_id: str,
    incoming_msg_id: str,
    incoming_text: str,
) -> None:
    """Drafts the LLM reply, sends it via the Threema SDK, and logs the full cycle."""
    if isinstance(app_or_connection, web.Application):
        connection: Connection = app_or_connection["threema_connection"]
        audit_file: Optional[TextIO] = app_or_connection.get("audit_file")
    else:
        connection = app_or_connection
        audit_file = None

    # Step 2: Draft reply
    reply_text = await draft_llm_response(incoming_text, sender_id)

    # Step 3: Send via Threema Gateway SDK (uses cached public key automatically)
    outgoing_msg_id = None
    status = "success"
    error_detail = None

    try:
        logger.info(f"[Step 3] Sending E2E reply to {sender_id} via Threema SDK...")
        outgoing_message = TextMessage(
            connection=connection,
            to_id=sender_id,
            text=reply_text,
        )
        outgoing_msg_id = await outgoing_message.send()
        logger.info(
            f"[Step 3] Message sent successfully! Outgoing Message ID: {outgoing_msg_id}"
        )
    except GatewayError as exc:
        status = "gateway_error"
        error_detail = str(exc)
        logger.error(f"[Step 3] Threema GatewayError sending to {sender_id}: {exc}")
    except Exception as exc:
        status = "unexpected_error"
        error_detail = str(exc)
        logger.error(
            f"[Step 3] Unexpected error sending to {sender_id}: {exc}", exc_info=True
        )

    # Step 4: Log the completed action
    audit_entry = {
        "status": status,
        "sender_id": sender_id,
        "gateway_id": THREEMA_ID,
        "incoming_message_id": incoming_msg_id,
        "incoming_text": incoming_text,
        "llm_model": OPENAI_MODEL,
        "drafted_reply": reply_text,
        "outgoing_message_id": outgoing_msg_id,
        "error": error_detail,
    }
    write_audit_log(audit_file, audit_entry)
    logger.info(f"[Step 4] Action logged to interactions.jsonl (status={status}).")


# ---------------------------------------------------------------------------
# Step 1: Receive Message via the SDK (Webhook Endpoint)
# ---------------------------------------------------------------------------
async def threema_webhook_handler(request: web.Request) -> web.Response:
    """
    Receives the POST callback from Threema Gateway (via ngrok), deduplicates,
    validates the MAC, and decrypts the message.
    """
    connection: Connection = request.app["threema_connection"]
    seen_ids: deque = request.app.get("seen_message_ids", deque(maxlen=512))
    bg_tasks: set[asyncio.Task] = request.app.get("bg_tasks", set())

    post_data = await request.post()
    parameters = dict(post_data)
    sender_id = parameters.get("from", "UNKNOWN")
    incoming_msg_id = parameters.get("messageId", "UNKNOWN")

    # Fast deduplication against ngrok / Threema webhook retries
    if incoming_msg_id != "UNKNOWN" and incoming_msg_id in seen_ids:
        logger.info(f"[Step 1] Duplicate messageId={incoming_msg_id} ignored.")
        return web.Response(status=200, text="OK")

    logger.info(
        f"[Step 1] Webhook hit from {sender_id} (messageId={incoming_msg_id}). Unpacking..."
    )

    try:
        message = await unpack_message(connection, parameters)
    except GatewayError as exc:
        logger.error(f"[Step 1] Failed to unpack/verify Threema message: {exc}")
        return web.Response(status=400, text="Bad Request / Verification Failed")
    except Exception as exc:
        logger.error(f"[Step 1] Error unpacking message: {exc}", exc_info=True)
        return web.Response(status=500, text="Internal Server Error")

    # Record messageId only after HMAC verification succeeds
    if incoming_msg_id != "UNKNOWN":
        seen_ids.append(incoming_msg_id)

    # Filter message types:
    # Important! When a user receives or reads your bot's message, their app sends a
    # DeliveryReceipt to this exact same webhook. Ignore receipts so we don't loop!
    if (
        isinstance(message, DeliveryReceipt)
        or isinstance(message, getattr(DeliveryReceipt, "__wrapped__", DeliveryReceipt))
        or type(message).__name__ == "DeliveryReceipt"
    ):
        receipt_type = getattr(message, "receipt_type", None)
        receipt_name = getattr(receipt_type, "name", "UNKNOWN")
        logger.info(
            f"[Step 1] Received DeliveryReceipt ({receipt_name}) from {sender_id}. Ignoring."
        )
        return web.Response(status=200, text="OK")

    if (
        isinstance(message, TextMessage)
        or isinstance(message, getattr(TextMessage, "__wrapped__", TextMessage))
        or type(message).__name__ == "TextMessage"
        or hasattr(message, "text")
    ):
        incoming_text = getattr(message, "text", "")
        logger.info(f"[Step 1] Decrypted TextMessage from {sender_id}: {incoming_text!r}")

        # Retain a strong reference to the background task so GC cannot collect it
        task = asyncio.create_task(
            process_and_reply(
                app_or_connection=request.app,
                sender_id=sender_id,
                incoming_msg_id=incoming_msg_id,
                incoming_text=incoming_text,
            )
        )
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return web.Response(status=200, text="OK")

    logger.info(
        f"[Step 1] Received non-text message ({type(message).__name__}) from {sender_id}."
    )
    return web.Response(status=200, text="OK")


# ---------------------------------------------------------------------------
# Application Lifecycle Management
# ---------------------------------------------------------------------------
async def on_startup(app: web.Application) -> None:
    """Initializes the Threema Connection, Public Key Cache, and persistent resources."""
    if not all([THREEMA_ID, THREEMA_SECRET, THREEMA_PRIVATE_KEY]):
        raise RuntimeError(
            "Missing THREEMA_GATEWAY_ID, THREEMA_GATEWAY_SECRET, or THREEMA_PRIVATE_KEY in .env"
        )

    connection = Connection(
        identity=THREEMA_ID,
        secret=THREEMA_SECRET,
        key=THREEMA_PRIVATE_KEY,
    )

    # Inject an in-memory async cache around connection.get_public_key
    # Eliminates redundant HTTP GET /pubkeys/{id} requests on receive & send
    orig_get_public_key = connection.get_public_key
    pubkey_cache: dict[str, object] = {}

    async def cached_get_public_key(identity: str):
        key = pubkey_cache.get(identity)
        if key is None:
            key = await orig_get_public_key(identity)
            pubkey_cache[identity] = key
        return key

    connection.get_public_key = cached_get_public_key

    app["threema_connection"] = connection
    app["pubkey_cache"] = pubkey_cache
    app["seen_message_ids"] = deque(maxlen=512)
    app["bg_tasks"] = set()
    app["audit_file"] = open("interactions.jsonl", "a", encoding="utf-8")

    logger.info(
        f"Initialized Threema Gateway SDK Connection for ID: {THREEMA_ID} (with pubkey cache)"
    )


async def on_cleanup(app: web.Application) -> None:
    """Drains in-flight reply tasks, then closes network sessions and file streams."""
    bg_tasks: set[asyncio.Task] = app.get("bg_tasks", set())
    if bg_tasks:
        logger.info(f"Waiting for {len(bg_tasks)} in-flight reply task(s) to finish...")
        await asyncio.gather(*bg_tasks, return_exceptions=True)

    connection: Connection = app.get("threema_connection")
    if connection:
        await connection.close()
        logger.info("Closed Threema Gateway SDK Connection.")

    await llm_client.close()

    audit_file: Optional[TextIO] = app.get("audit_file")
    if audit_file and not getattr(audit_file, "closed", True):
        audit_file.close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_post("/webhook/threema", threema_webhook_handler)
    app.router.add_post("/gateway_callback", threema_webhook_handler)
    return app


if __name__ == "__main__":
    logger.info(f"Starting local Threema webhook server on port {PORT}...")
    web.run_app(create_app(), host="127.0.0.1", port=PORT, access_log=None)
