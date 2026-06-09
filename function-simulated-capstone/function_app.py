"""
Smart Support Ticket Pipeline - Azure Function App
===================================================
A single blob-triggered function that orchestrates the full pipeline:
  Blob Storage (ingest) → Azure OpenAI (classify) → Cosmos DB (persist)

Key architecture decisions:
  - Uses the OpenAI v1 API (base_url with /openai/v1/) required for GPT-5 family models
  - Uses Pydantic structured outputs instead of prompt-based JSON parsing
  - Implements tenacity exponential backoff for 429 rate-limit resilience
  - Uses upsert_item for idempotent Cosmos DB writes (safe for retried executions)

Environment variables required (set in Settings → Environment Variables → App Settings):
  FOUNDRY_ENDPOINT        - e.g. https://your-resource.openai.azure.com
  FOUNDRY_API_KEY         - from Keys & Endpoint blade in Azure Portal
  FOUNDRY_DEPLOYMENT      - your deployment name (e.g. "gpt-5-mini")
  COSMOS_ENDPOINT         - e.g. https://your-cosmos.documents.azure.com:443/
  COSMOS_KEY              - from Keys blade in Azure Portal
  COSMOS_DATABASE         - e.g. "SupportDB"
  COSMOS_CONTAINER        - e.g. "Tickets"
  EMAILSTORAGE_CONNECTION - connection string for the blob storage account (in storage account Keys)
"""

import azure.functions as func
import logging
import os
from datetime import datetime, timezone

from openai import OpenAI, RateLimitError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    wait_random_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)
from azure.cosmos import CosmosClient

# ---------------------------------------------------------------------------
# Initialize the Function App
# ---------------------------------------------------------------------------
app = func.FunctionApp()


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
    endpoint = os.environ["FOUNDRY_ENDPOINT"]
    api_key = os.environ["FOUNDRY_API_KEY"]

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
        reasoning_effort="low",
        max_completion_tokens=3000,   # Keep tight but leave enough for reasoning tokens.
    )

    result = completion.choices[0].message.parsed

    if result is None:
        # Model refused or returned unparseable content
        refusal = completion.choices[0].message.refusal
        raise ValueError(f"LLM refused to process ticket: {refusal}")

    return result


# ---------------------------------------------------------------------------
# Main blob-triggered function
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
    deployment_name = os.environ["FOUNDRY_DEPLOYMENT"]
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
    ticket_id = blob_name.split("/")[-1]
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