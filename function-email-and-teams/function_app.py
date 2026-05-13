"""
Smart Support Ticket Pipeline - Azure Function App
===================================================
A unified Function App containing the full pipeline plus email integration:

  PART 1 (Existing Pipeline):
    Blob Storage (ingest) → Azure OpenAI (classify) → Cosmos DB (persist)

  PART 2 (Email Integration — Course 1 Video Demo):
    M365 Mailbox → Graph webhook → Blob Storage → [triggers Part 1] → Graph reply

Functions registered in this app:
  1. process_support_ticket  (Blob Trigger)          — AI classification pipeline
  2. subscription_manager    (Timer Trigger)          — Creates/renews Graph mail subscription
  3. webhook_receiver        (HTTP Trigger)           — Receives Graph notifications, writes blobs
  4. reply_sender            (Cosmos DB Change Feed)  — Sends replies or escalates to Teams

Key architecture decisions:
  - Uses the OpenAI v1 API (base_url with /openai/v1/) required for GPT-5 family models
  - Uses Pydantic structured outputs instead of prompt-based JSON parsing
  - Implements tenacity exponential backoff for 429 rate-limit resilience
  - Uses upsert_item for idempotent Cosmos DB writes (safe for retried executions)

Environment variables required (set in Settings → Environment Variables → App Settings):
  --- Existing pipeline ---
  AZURE_OPENAI_ENDPOINT     - e.g. https://your-resource.openai.azure.com
  AZURE_OPENAI_API_KEY      - from Keys & Endpoint blade in Azure Portal
  AZURE_OPENAI_DEPLOYMENT   - your deployment name (e.g. "gpt-5-mini")
  COSMOS_ENDPOINT           - e.g. https://your-cosmos.documents.azure.com:443/
  COSMOS_KEY                - from Keys blade in Azure Portal
  COSMOS_DATABASE           - e.g. "SupportDB"
  COSMOS_CONTAINER          - e.g. "Tickets"
  EMAILSTORAGE_CONNECTION   - connection string for the blob storage account (in storage account Keys)

  --- Graph email integration ---
  GRAPH_TENANT_ID           - Entra tenant ID (from Overview blade)
  GRAPH_CLIENT_ID           - App registration client ID
  GRAPH_CLIENT_SECRET       - App registration client secret value
  SUPPORT_MAILBOX_USER_ID   - Object ID of the support mailbox user in Entra ID
  WEBHOOK_URL               - Public HTTPS URL of webhook_receiver function (with function key)
  GRAPH_CLIENT_STATE        - Random shared secret for notification validation
  COSMOS_CONNECTION_STRING  - Full Cosmos DB connection string (for change feed trigger)
  TEAMS_WEBHOOK_URL         - (Optional) Teams incoming webhook URL for critical alerts
"""

import azure.functions as func
import logging
import os
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from openai import OpenAI, RateLimitError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    wait_random_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)
from azure.cosmos import CosmosClient, PartitionKey
from azure.storage.blob import BlobServiceClient

# Graph integration dependencies
import msal
import requests as http_requests  # Aliased to avoid collision with func parameter names

# ---------------------------------------------------------------------------
# Initialize the Function App
# ---------------------------------------------------------------------------
app = func.FunctionApp()


# ===================================================================
# PART 1: EXISTING AI PIPELINE
# ===================================================================
# Blob Storage (ingest) → Azure OpenAI (classify) → Cosmos DB (persist)
# This section is unchanged from the original pipeline implementation.
# ===================================================================


# ---------------------------------------------------------------------------
# Pydantic model for structured LLM output
# ---------------------------------------------------------------------------
# This model enforces the JSON schema at the API level. The LLM is
# constrained to return ONLY these fields in this exact shape. No prompt-
# based JSON parsing required — the SDK validates the response for you.
# ---------------------------------------------------------------------------
class TicketAnalysis(BaseModel):
    """Structured output schema for support ticket analysis."""
    sender_email: str = Field(description="Email address extracted from the From: header of the ticket")
    subject: str = Field(description="Subject line extracted from the Subject: header of the ticket")
    category: str = Field(description="Issue category, e.g. 'Billing', 'Technical', 'Account Access'")
    sentiment: str = Field(description="Detected sentiment: 'positive', 'neutral', 'negative', 'angry'")
    severity: str = Field(description="Severity level: 'low', 'medium', 'high', 'critical'")
    root_causes: list[str] = Field(description="List of potential root causes identified in the ticket")
    draft_response: str | None = Field(
        description="A polite draft response email. Set to null if severity is critical."
    )
    is_critical: bool = Field(
        description="True if the ticket contains legal threats, extreme anger, or safety concerns"
    )


# ---------------------------------------------------------------------------
# Azure OpenAI client setup (v1 API)
# ---------------------------------------------------------------------------
# IMPORTANT: GPT-5 family models require the v1 API endpoint format.
# We use the standard OpenAI() client (NOT AzureOpenAI) with base_url
# pointed to: https://<resource>.openai.azure.com/openai/v1/
#
# The AzureOpenAI client uses the legacy /deployments/ URL pattern which
# is incompatible with the v1 API path. Using OpenAI() with base_url
# avoids this issue entirely.
# ---------------------------------------------------------------------------
def get_openai_client() -> OpenAI:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key = os.environ["AZURE_OPENAI_API_KEY"]

    # Ensure the base_url ends with /openai/v1/
    base_url = endpoint.rstrip("/") + "/openai/v1/"

    return OpenAI(
        base_url=base_url,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Cosmos DB client setup
# ---------------------------------------------------------------------------
def get_cosmos_container():
    client = CosmosClient(
        url=os.environ["COSMOS_ENDPOINT"],
        credential=os.environ["COSMOS_KEY"],
    )
    database = client.get_database_client(os.environ["COSMOS_DATABASE"])
    container = database.get_container_client(os.environ["COSMOS_CONTAINER"])
    return container


# ---------------------------------------------------------------------------
# LLM call with tenacity retry
# ---------------------------------------------------------------------------
# retry_if_exception_type(RateLimitError):
#   ONLY retries on 429s. Auth errors, bad requests, etc. fail immediately.
#   This is critical — retrying a 401 will never succeed and wastes time.
#
# wait_random_exponential(min=1, max=60):
#   Adds jitter to prevent "thundering herd" if multiple functions hit
#   the rate limit simultaneously.
#
# stop_after_attempt(6):
#   Caps total attempts. With exponential backoff up to 60s, this gives
#   the TPM quota ~2 minutes to reset (Azure OpenAI resets per minute).
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert Helpdesk Support Agent for an enterprise IT organization.

Analyze the support ticket provided and produce a structured analysis.

Classification rules:
- Categorize into one of: Billing, Technical, Account Access, Feature Request, Security, Other
- Assess sentiment as: positive, neutral, negative, or angry
- Assess severity as: low, medium, high, or critical
- If the ticket contains legal threats, mentions of lawyers/lawsuits, extreme profanity,
  or threats of harm, set is_critical to true and severity to "critical"
- If is_critical is true, set draft_response to null (do NOT generate an automated reply)
- If is_critical is false, generate a polite, professional draft response email

Be concise. Focus on actionable root cause identification."""


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_random_exponential(min=1, max=60),
    stop=stop_after_attempt(6),
    before_sleep=lambda retry_state: logging.warning(
        f"Rate limited (429). Retrying in {retry_state.next_action.sleep:.1f}s "
        f"(attempt {retry_state.attempt_number}/6)"
    ),
)
def call_llm(client: OpenAI, ticket_text: str, deployment_name: str) -> TicketAnalysis:
    """Call Azure OpenAI with structured output and retry logic."""
    completion = client.beta.chat.completions.parse(
        model=deployment_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": ticket_text},
        ],
        response_format=TicketAnalysis,
        max_completion_tokens=2000,   # Keep tight but leave enough for reasoning tokens.
    )

    result = completion.choices[0].message.parsed

    if result is None:
        # Model refused or returned unparseable content
        refusal = completion.choices[0].message.refusal
        raise ValueError(f"LLM refused to process ticket: {refusal}")

    return result


# ---------------------------------------------------------------------------
# FUNCTION 1: Blob-triggered AI pipeline
# ---------------------------------------------------------------------------
# path: must match your blob container name exactly
# connection: references the EMAILSTORAGE_CONNECTION app setting by default,
#   or a custom app setting name pointing to your storage connection string
# ---------------------------------------------------------------------------
@app.blob_trigger(
    arg_name="myblob",
    path="support-tickets/{name}",
    connection="EMAILSTORAGE_CONNECTION",
)
def process_support_ticket(myblob: func.InputStream):
    """
    Triggered when a new file is uploaded to the 'support-tickets' container.
    Reads the file, sends it to Azure OpenAI for analysis, and persists
    the structured result to Cosmos DB.
    """
    blob_name = myblob.name or "unknown"
    logging.info(f"[INGEST] Processing blob: {blob_name} ({myblob.length} bytes)")
    if not blob_name.lower().endswith(".txt"):
        logging.info(f"[INGEST] Skipping non-.txt file: {blob_name}")
        return

    # ---- Step 1: Read blob content ----
    try:
        ticket_text = myblob.read().decode("utf-8")
    except Exception as e:
        logging.error(f"[INGEST] Failed to read blob {blob_name}: {e}")
        raise

    if not ticket_text.strip():
        logging.warning(f"[INGEST] Empty blob: {blob_name}. Skipping.")
        return

    logging.info(f"[INGEST] Read {len(ticket_text)} characters from {blob_name}")

    # ---- Step 2: Call Azure OpenAI ----
    deployment_name = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    openai_client = get_openai_client()

    try:
        analysis = call_llm(openai_client, ticket_text, deployment_name)
    except RateLimitError:
        logging.error(f"[AI] Exhausted all retry attempts for {blob_name}. Rate limit not cleared.")
        raise
    except Exception as e:
        logging.error(f"[AI] LLM call failed for {blob_name}: {e}")
        raise

    logging.info(
        f"[AI] Analysis complete: category={analysis.category}, "
        f"sentiment={analysis.sentiment}, severity={analysis.severity}, "
        f"is_critical={analysis.is_critical}"
    )

    # ---- Step 3: Handle critical ticket escalation ----
    if analysis.is_critical:
        logging.warning(
            f"[ESCALATION] CRITICAL TICKET DETECTED: {blob_name} — "
            f"Category: {analysis.category}, Sentiment: {analysis.sentiment}. "
            f"Suppressing auto-generated response. Flagging for human review."
        )

    # ---- Step 4: Build Cosmos DB document ----
    ticket_id = str(uuid.uuid4())
    document = {
        "id": ticket_id,                          # Required by Cosmos DB
        "sender_email": analysis.sender_email,
        "subject": analysis.subject,
        "category": analysis.category,             # Also serves as partition key
        "sentiment": analysis.sentiment,
        "severity": analysis.severity,
        "root_causes": analysis.root_causes,
        "draft_response": analysis.draft_response,
        "is_critical": analysis.is_critical,
        "source_blob": blob_name,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "raw_text_preview": ticket_text[:500],     # Store a preview, not full text
    }

    # Add priority flag for critical tickets (capstone requirement)
    if analysis.is_critical:
        document["priority"] = "critical"

    # ---- Step 5: Upsert to Cosmos DB ----
    try:
        container = get_cosmos_container()
        container.upsert_item(document)
        logging.info(f"[DB] Upserted document {ticket_id} to Cosmos DB")
    except Exception as e:
        logging.error(f"[DB] Cosmos DB upsert failed for {blob_name}: {e}")
        raise

    logging.info(
        f"[COMPLETE] Pipeline finished for {blob_name} → "
        f"id={ticket_id}, category={analysis.category}, "
        f"critical={analysis.is_critical}"
    )


# ===================================================================
# PART 2: GRAPH EMAIL INTEGRATION (Course 1 Video Demo)
# ===================================================================
# These functions connect a Microsoft 365 mailbox to the pipeline above,
# closing the loop from inbound email to AI-generated reply.
#
# Flow:
#   Graph change notification → webhook_receiver → writes .txt + .meta.json
#   to support-tickets container → blob trigger fires process_support_ticket
#   → Cosmos DB document created → change feed fires reply_sender → Graph
#   reply API sends response on original email thread.
#
# The .meta.json sidecar stores Graph-specific identifiers (messageId,
# conversationId) that reply_sender needs to thread the reply correctly.
# The blob trigger ignores .meta.json files because process_support_ticket
# only reads .txt content.
# ===================================================================


# ---------------------------------------------------------------------------
# Graph helpers (shared by subscription_manager, webhook, reply_sender)
# ---------------------------------------------------------------------------

_msal_app = None


def _get_msal_app():
    """Return a cached MSAL ConfidentialClientApplication instance.

    MSAL handles token caching internally. The global instance persists
    within a single cold-start of the Python worker process, so repeated
    function invocations reuse the same token until it expires.
    """
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=os.environ["GRAPH_CLIENT_ID"],
            authority="https://login.microsoftonline.com/" + os.environ["GRAPH_TENANT_ID"],
            client_credential=os.environ["GRAPH_CLIENT_SECRET"],
        )
    return _msal_app


def _get_graph_token():
    """Acquire a Graph API access token via client credentials flow.

    Uses the .default scope, which for client credentials means
    "all application permissions granted via admin consent."
    """
    result = _get_msal_app().acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(
            "Token acquisition failed: " + result.get("error_description", str(result))
        )
    return result["access_token"]


def _graph_headers():
    """Return standard headers for Graph REST calls."""
    return {
        "Authorization": "Bearer " + _get_graph_token(),
        "Content-Type": "application/json",
    }


def _get_blob_service_client():
    """Return a BlobServiceClient using the email storage connection string."""
    return BlobServiceClient.from_connection_string(os.environ["EMAILSTORAGE_CONNECTION"])


def _get_ticket_container_client():
    """Return a ContainerClient for the support-tickets container."""
    return _get_blob_service_client().get_container_client("support-tickets")


# ---------------------------------------------------------------------------
# FUNCTION 2: Subscription Manager (Timer-Triggered)
# ---------------------------------------------------------------------------
# Runs every 30 minutes. Creates or renews the Graph mail subscription
# so the webhook stays active continuously.
#
# Graph mail subscriptions have a max lifetime of 4230 minutes (~2.9 days).
# A 30-minute cadence means even if several renewals fail consecutively,
# the subscription won't expire before the next successful renewal.
#
# The current subscription ID is persisted in a small blob in the
# "function-state" container so it survives function restarts and
# cold starts across multiple timer invocations.
# ---------------------------------------------------------------------------

_SUB_RECEIPT_CONTAINER = "function-state"
_SUB_RECEIPT_BLOB = "graph-subscription-id.txt"


@app.timer_trigger(
    schedule="0 */30 * * * *",
    arg_name="timer",
    run_on_startup=False,
)
def subscription_manager(timer: func.TimerRequest):
    """Create or renew the Microsoft Graph change-notification subscription."""
    logging.info(f"subscription_manager: invoked at {datetime.now(timezone.utc).isoformat()}")

    mailbox_id = os.environ["SUPPORT_MAILBOX_USER_ID"]
    webhook_url = os.environ["WEBHOOK_URL"]
    client_state = os.environ["GRAPH_CLIENT_STATE"]

    # Request expiration 4200 min from now (under the 4230 cap)
    expiration = (
        datetime.now(timezone.utc) + timedelta(minutes=4200)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    # --- Try to renew an existing subscription ---
    existing_id = _load_subscription_id()
    if existing_id:
        renewed = _renew_subscription(existing_id, expiration)
        if renewed:
            logging.info(f"subscription_manager: renewed subscription {existing_id}")
            return

    # --- Create a new subscription ---
    payload = {
        "changeType": "created",
        "notificationUrl": webhook_url,
        "lifecycleNotificationUrl": webhook_url,
        "resource": f"users/{mailbox_id}/mailFolders('Inbox')/messages",
        "expirationDateTime": expiration,
        "clientState": client_state,
    }

    resp = http_requests.post(
        "https://graph.microsoft.com/v1.0/subscriptions",
        headers=_graph_headers(),
        json=payload,
        timeout=30,
    )

    if resp.status_code == 201:
        sub = resp.json()
        _save_subscription_id(sub["id"])
        logging.info(
            f"subscription_manager: created subscription {sub['id']}, "
            f"expires {sub['expirationDateTime']}"
        )
    else:
        logging.error(
            f"subscription_manager: create failed - {resp.status_code} {resp.text}"
        )


def _renew_subscription(subscription_id, expiration):
    """PATCH the subscription to extend its expiration. Returns True on success."""
    resp = http_requests.patch(
        f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
        headers=_graph_headers(),
        json={"expirationDateTime": expiration},
        timeout=30,
    )
    if resp.status_code == 200:
        return True
    logging.warning(
        f"subscription_manager: renewal failed for {subscription_id} - "
        f"{resp.status_code} {resp.text}"
    )
    return False


def _load_subscription_id():
    """Read the current subscription ID from blob storage, if any."""
    try:
        blob_service = _get_blob_service_client()
        container = blob_service.get_container_client(_SUB_RECEIPT_CONTAINER)
        blob = container.get_blob_client(_SUB_RECEIPT_BLOB)
        return blob.download_blob().readall().decode("utf-8").strip()
    except Exception:
        return None


def _save_subscription_id(subscription_id):
    """Persist the subscription ID so future timer runs can renew it."""
    try:
        blob_service = _get_blob_service_client()
        container = blob_service.get_container_client(_SUB_RECEIPT_CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass  # already exists
        blob = container.get_blob_client(_SUB_RECEIPT_BLOB)
        blob.upload_blob(subscription_id.encode("utf-8"), overwrite=True)
    except Exception as exc:
        logging.error(f"Failed to persist subscription ID: {exc}")


# ---------------------------------------------------------------------------
# FUNCTION 3: Webhook Receiver (HTTP-Triggered)
# ---------------------------------------------------------------------------
# Handles three types of inbound POST from Microsoft Graph:
#
#   a) Validation handshake — Graph sends ?validationToken=<token> when
#      creating a subscription. We must echo the token in plain text.
#
#   b) Lifecycle notifications — e.g. "reauthorizationRequired" when the
#      subscription is about to expire or needs token refresh.
#
#   c) Change notifications — a new email arrived. We fetch the full
#      message from Graph, convert HTML to plain text, and write two blobs:
#        - {messageId}.txt       → triggers process_support_ticket
#        - {messageId}.meta.json → sidecar with Graph IDs for reply threading
#
# Note on .meta.json and the blob trigger:
#   The blob trigger fires for BOTH .txt and .meta.json uploads. However,
#   process_support_ticket reads the blob content as UTF-8 text and sends
#   it to the LLM. The .meta.json blob will be processed by the LLM as
#   a "ticket" — it will produce a nonsensical analysis. This is acceptable
#   for the demo because:
#     1. The resulting Cosmos document is harmless (garbage category/sentiment)
#     2. reply_sender won't find a matching .meta.json for a .meta.json source
#        blob, so no reply email is sent
#   For production, add a filename filter (e.g., skip if not .txt).
# ---------------------------------------------------------------------------


@app.route(
    route="webhook",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def webhook_receiver(req: func.HttpRequest) -> func.HttpResponse:
    """Handle Microsoft Graph change-notification webhook deliveries."""

    # --- (a) Validation handshake ---
    validation_token = req.params.get("validationToken")
    if validation_token:
        logging.info("webhook_receiver: responding to validation handshake")
        return func.HttpResponse(
            validation_token,
            status_code=200,
            mimetype="text/plain",
        )

    # --- Parse notification payload ---
    try:
        body = req.get_json()
    except ValueError:
        logging.warning("webhook_receiver: non-JSON payload received")
        return func.HttpResponse(status_code=400)

    notifications = body.get("value", [])
    client_state = os.environ.get("GRAPH_CLIENT_STATE", "")

    for notification in notifications:
        # --- (b) Lifecycle notifications ---
        lifecycle_event = notification.get("lifecycleEvent")
        if lifecycle_event:
            logging.info(
                f"webhook_receiver: lifecycle event '{lifecycle_event}' "
                f"for sub {notification.get('subscriptionId')}"
            )
            if lifecycle_event == "reauthorizationRequired":
                _handle_reauthorization(notification.get("subscriptionId"))
            continue

        # --- (c) Change notifications ---
        if notification.get("clientState") != client_state:
            logging.warning("webhook_receiver: clientState mismatch — ignoring")
            continue

        resource_path = notification.get("resource", "")
        logging.info(f"webhook_receiver: processing notification for {resource_path}")

        try:
            _process_email_notification(resource_path)
        except Exception as exc:
            logging.error(f"webhook_receiver: failed to process {resource_path} - {exc}")

    # Respond 202 promptly so Graph does not retry
    return func.HttpResponse(status_code=202)


def _handle_reauthorization(subscription_id):
    """Attempt to renew a subscription flagged for reauth."""
    if not subscription_id:
        return
    expiration = (
        datetime.now(timezone.utc) + timedelta(minutes=4200)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    _renew_subscription(subscription_id, expiration)


def _process_email_notification(resource_path):
    """Fetch the full email from Graph and write it to blob storage.

    Args:
        resource_path: Graph resource from the notification, e.g.
            "users/{id}/mailFolders('Inbox')/messages/{messageId}"
    """
    mailbox_id = os.environ["SUPPORT_MAILBOX_USER_ID"]

    # Extract messageId from the resource path (last segment)
    message_id = resource_path.rstrip("/").split("/")[-1]

    container = _get_ticket_container_client()

    # --- Deduplication: skip if blob already exists ---
    # Graph delivers notifications at-least-once, so the same messageId
    # can arrive multiple times. Checking blob existence prevents duplicate
    # pipeline runs for the same email.
    blob_name = message_id + ".txt"
    blob_client = container.get_blob_client(blob_name)
    if _blob_exists(blob_client):
        logging.info(f"webhook_receiver: blob exists for {message_id} — skipping")
        return

    # --- Fetch the full message from Graph ---
    select_fields = (
        "id,subject,from,sender,toRecipients,receivedDateTime,"
        "body,bodyPreview,conversationId,internetMessageId"
    )
    url = (
        f"https://graph.microsoft.com/v1.0/users/{mailbox_id}"
        f"/messages/{message_id}?$select={select_fields}"
    )
    resp = http_requests.get(url, headers=_graph_headers(), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Graph GET message failed: {resp.status_code} {resp.text}")

    message = resp.json()

    # --- Extract plain-text body ---
    body_obj = message.get("body", {})
    body_content = body_obj.get("content", "")
    content_type = body_obj.get("contentType", "text")

    if content_type.lower() == "html":
        plain_text = _strip_html(body_content)
    else:
        plain_text = body_content

    if not plain_text.strip():
        plain_text = message.get("bodyPreview", "(empty message)")

    # --- Build the ticket text file ---
    # Format matches what the LLM system prompt expects: headers then body
    subject = message.get("subject", "(no subject)")
    sender_addr = (
        message.get("from", {}).get("emailAddress", {}).get("address", "unknown")
    )
    received = message.get("receivedDateTime", "")

    support_addr = os.environ.get("SUPPORT_MAILBOX_ADDRESS", "it-support@ctrl-alt-sweets.com").lower()
    if sender_addr.lower() == support_addr:
        logging.info(f"webhook_receiver: ignoring self-sent email from {sender_addr}")
        return

    ticket_text = (
        f"Subject: {subject}\n"
        f"From: {sender_addr}\n"
        f"Received: {received}\n"
        f"---\n"
        f"{plain_text}"
    )

    # --- Upload ticket text to blob storage ---
    # overwrite=False ensures deduplication even under race conditions
    blob_client.upload_blob(ticket_text.encode("utf-8"), overwrite=False)
    logging.info(f"webhook_receiver: wrote {blob_name} to support-tickets")

    # --- Write sidecar metadata JSON ---
    # This is NOT processed by the LLM — it's read by reply_sender
    # to find the original Graph messageId for threading the reply.
    metadata = {
        "messageId": message.get("id"),
        "internetMessageId": message.get("internetMessageId"),
        "conversationId": message.get("conversationId"),
        "subject": subject,
        "senderAddress": sender_addr,
        "receivedDateTime": received,
    }

    meta_blob_name = message_id + ".meta.json"
    meta_blob_client = container.get_blob_client(meta_blob_name)
    meta_blob_client.upload_blob(
        json.dumps(metadata, indent=2).encode("utf-8"),
        overwrite=True,
    )
    logging.info(f"webhook_receiver: wrote {meta_blob_name} sidecar metadata")


def _blob_exists(blob_client):
    """Check whether a blob exists without downloading it."""
    try:
        blob_client.get_blob_properties()
        return True
    except Exception:
        return False


def _strip_html(html):
    """Minimalist HTML-to-text conversion.

    For production, use beautifulsoup4 or html2text. This regex-based
    approach is intentionally kept simple for the demo to avoid adding
    heavy dependencies. It handles the common patterns in Outlook-
    generated HTML (inline styles, <br>, <p>/<div> blocks).
    """
    # Remove <style> and <script> blocks
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li)>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# FUNCTION 4: Reply Sender (Cosmos DB Change Feed-Triggered)
# ---------------------------------------------------------------------------
# Watches for new/updated documents in the Tickets container via the
# Cosmos DB change feed. When the AI pipeline writes a new ticket:
#
#   - If draft_response exists AND priority != "critical":
#       → Reply on the original email thread via Graph messages/{id}/reply
#   - If priority IS "critical":
#       → Post an escalation alert to Teams via incoming webhook
#   - If no draft_response:
#       → Skip (nothing to send)
#
# The reply_sender correlates back to the original email using:
#   1. source_blob field from the Cosmos document (e.g., "support-tickets/{messageId}.txt")
#   2. The .meta.json sidecar blob written by webhook_receiver
#   3. The Graph messageId inside the sidecar, used for the reply API
#
# COSMOS_CONNECTION_STRING is required here because the change feed
# trigger binding uses a connection string (not endpoint + key).
# This is separate from the COSMOS_ENDPOINT/COSMOS_KEY used by the
# pipeline's SDK calls — both point to the same account.
# ---------------------------------------------------------------------------


@app.cosmos_db_trigger(
    arg_name="documents",
    container_name="processed-tickets",
    database_name="smart-support",
    connection="COSMOS_CONNECTION_STRING",
    lease_container_name="leases",
    create_lease_container_if_not_exists=True,
)
def reply_sender(documents: func.DocumentList):
    """Send LLM-generated replies via Graph on the original email thread."""
    mailbox_id = os.environ["SUPPORT_MAILBOX_USER_ID"]
    container = _get_ticket_container_client()

    for doc in documents:
        # Handle both possible document formats from the binding
        if hasattr(doc, "to_dict"):
            ticket = doc.to_dict()
        else:
            ticket = json.loads(doc.to_json())

        ticket_id = ticket.get("id", "unknown")
        priority = ticket.get("priority", "")
        draft_response = ticket.get("draft_response")

        logging.info(f"reply_sender: processing ticket {ticket_id} (priority={priority})")

        # --- Critical tickets: do NOT auto-reply ---
        if str(priority).lower() == "critical":
            logging.warning(f"reply_sender: ticket {ticket_id} is CRITICAL — suppressing auto-reply")
            _post_teams_alert(ticket)
            continue

        # --- No draft: nothing to send ---
        if not draft_response:
            logging.info(f"reply_sender: ticket {ticket_id} has no draft_response — skipping")
            continue

        # --- Extract original messageId from source_blob ---
        # source_blob is stored as "support-tickets/{messageId}.txt" by the pipeline.
        # We need just the messageId portion to find the .meta.json sidecar.
        source_blob = ticket.get("source_blob", "")
        # Strip container path prefix if present, then strip .txt extension
        filename = source_blob.split("/")[-1] if "/" in source_blob else source_blob
        original_message_id = filename.replace(".txt", "") if filename.endswith(".txt") else None

        if not original_message_id:
            logging.warning(
                f"reply_sender: ticket {ticket_id} source_blob '{source_blob}' "
                f"doesn't match expected pattern — cannot reply"
            )
            continue

        # --- Load sidecar metadata ---
        meta_blob_name = original_message_id + ".meta.json"
        try:
            meta_client = container.get_blob_client(meta_blob_name)
            meta_raw = meta_client.download_blob().readall().decode("utf-8")
            meta = json.loads(meta_raw)
        except Exception as exc:
            logging.error(f"reply_sender: failed to load metadata {meta_blob_name} — {exc}")
            continue

        graph_message_id = meta.get("messageId")
        if not graph_message_id:
            logging.warning(f"reply_sender: metadata missing messageId for {ticket_id}")
            continue

        # --- Send the reply via Graph ---
        reply_url = (
            f"https://graph.microsoft.com/v1.0/users/{mailbox_id}"
            f"/messages/{graph_message_id}/reply"
        )
        html_body = draft_response.replace("\n", "<br>")
        reply_payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": html_body,
                }
            },
            "comment": "",
        }

        resp = http_requests.post(
            reply_url,
            headers=_graph_headers(),
            json=reply_payload,
            timeout=30,
        )

        if resp.status_code == 202:
            logging.info(
                f"reply_sender: sent reply for ticket {ticket_id} "
                f"on thread {meta.get('conversationId', '?')}"
            )
        else:
            logging.error(
                f"reply_sender: Graph reply failed for {ticket_id} — "
                f"{resp.status_code} {resp.text}"
            )


def _post_teams_alert(ticket):
    """Post an escalation alert to a Teams channel via incoming webhook.

    Uses an Adaptive Card format for rich display in Teams. If
    TEAMS_WEBHOOK_URL is not configured, logs a warning and returns.
    """
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logging.warning(
            "reply_sender: TEAMS_WEBHOOK_URL not configured — skipping Teams alert"
        )
        return

    ticket_id = ticket.get("id", "unknown")
    category = ticket.get("category", "uncategorized")
    subject = ticket.get("subject", "(no subject)")

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Large",
                            "weight": "Bolder",
                            "text": "CRITICAL Support Ticket Escalation",
                            "color": "Attention",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Ticket ID", "value": ticket_id},
                                {"title": "Category", "value": category},
                                {"title": "Subject", "value": subject},
                                {"title": "Priority", "value": "CRITICAL"},
                            ],
                        },
                        {
                            "type": "TextBlock",
                            "text": (
                                "This ticket was flagged for human review. "
                                "No automated response was sent."
                            ),
                            "wrap": True,
                        },
                    ],
                },
            }
        ],
    }

    try:
        resp = http_requests.post(webhook_url, json=card, timeout=15)
        if resp.status_code in (200, 202):
            logging.info(f"reply_sender: Teams alert posted for ticket {ticket_id}")
        else:
            logging.error(
                f"reply_sender: Teams webhook failed — {resp.status_code} {resp.text}"
            )
    except Exception as exc:
        logging.error(f"reply_sender: Teams webhook error — {exc}")