# azure-genai-ticket-pipeline

A teaching repository for the Pluralsight learning path **Build a Generative AI Solution with Azure**.

This repo contains the working code for a "Smart Support Ticket" pipeline that classifies inbound support tickets using Azure OpenAI (gpt-5-mini) and persists structured results to Cosmos DB. Two variants are included:

| Variant | Purpose | Course usage |
|---|---|---|
| [`function-simulated-no-email/`](./function-simulated-no-email) | Pipeline with manual blob upload as the trigger source | Course 2 hands-on labs |
| [`function-email-and-teams/`](./function-email-and-teams) | Same pipeline plus a Microsoft Graph integration for live email and Teams escalation | Course 2 video demo walkthrough |

## How it works

Both variants share the same core pipeline:

```
Blob Storage  →  Azure Function  →  Azure OpenAI (gpt-5-mini)  →  Cosmos DB
```

A `.txt` file lands in the `support-tickets` blob container. A blob-triggered Python function reads the content, calls Azure OpenAI via the v1 API with a Pydantic-typed response schema, and writes a structured document to Cosmos DB partitioned by ticket category.

The `function-email-and-teams` variant adds three functions around that core:

- **Subscription manager** (timer trigger) creates and renews a Microsoft Graph mail subscription so a real M365 mailbox can drive the pipeline.
- **Webhook receiver** (HTTP trigger) accepts Graph change notifications, fetches each new email, strips HTML, and writes a `.txt` + `.meta.json` pair to the blob container — which kicks off the core pipeline.
- **Reply sender** (Cosmos change feed trigger) reads new ticket documents and either sends a reply via Graph on the original email thread or posts a critical-ticket alert to Teams.

The `.meta.json` sidecar exists because the LLM-generated document in Cosmos DB doesn't carry the Graph `messageId` needed to thread the reply. The webhook stores the message identifiers in a sidecar blob keyed by message ID, and the reply sender reads it back during reply.

## Why two variants

The email integration is a Course 1 conceptual demo. It illustrates how the pipeline plugs into real-world inbound traffic but introduces App Registration setup, Graph permissions, public webhook exposure, and tenant configuration that aren't necessary to teach the AI pipeline itself.

The simulated variant skips all of that. Labs in Course 2 use manual blob uploads to drive the pipeline, which keeps learners focused on Azure Functions, Azure OpenAI, Pydantic structured outputs, and Cosmos DB.

## Architecture decisions

A few choices are worth flagging up front because they're load-bearing for the rest of the code:

- **OpenAI v1 API endpoint.** The gpt-5 family requires the v1 endpoint shape (`/openai/v1/`). The code uses the standard `OpenAI` client with `base_url` rather than `AzureOpenAI`, which targets the legacy `/deployments/` URL pattern.
- **Pydantic structured outputs.** The `TicketAnalysis` model is passed to `response_format`, which constrains the model to return valid JSON matching the schema. No prompt-based JSON instructions, no parsing fallbacks.
- **Tenacity exponential backoff on 429s.** Retries fire only on `RateLimitError`. Auth errors, bad requests, and schema validation failures fail fast.
- **`upsert_item` for Cosmos writes.** Functions retry on transient failures, so the write must be idempotent. Upsert handles re-runs without duplicates.
- **Cosmos partition key `/category`.** Categories produce a small fixed set of partition values, which spreads load reasonably for a teaching workload. Production workloads with skewed categories would want a different key.

## Prototyping vs. production

This code uses **API key authentication** for both Azure OpenAI and Cosmos DB. That's the prototyping path. The production path is Entra ID with managed identity and role assignments (Cognitive Services OpenAI User on the Foundry resource, and a data plane role on Cosmos). The prototyping path is documented and taught here because it isolates one variable at a time — Course 1 covers the rationale and the production migration.

## Repo layout

```
.
├── README.md                            ← you are here
├── .gitignore                           ← root, applies repo-wide
├── function-simulated-no-email/
│   ├── README.md                        ← variant-specific setup
│   ├── function_app.py
│   ├── host.json
│   └── requirements.txt
└── function-email-and-teams/
    ├── README.md                        ← variant-specific setup
    ├── function_app.py
    ├── host.json
    └── requirements.txt
```

`local.settings.json` is intentionally not present. Both variants are designed to run in Azure — settings are configured under the Function App's **Environment Variables → App Settings** blade, not locally. See each variant's README for the required values.

## Required Azure resources

Both variants need:

- Azure Function App (Flex Consumption recommended for Foundry-region prototyping)
- Azure Storage Account (for function runtime + the `support-tickets` container)
- Azure AI Foundry resource with a gpt-5-mini deployment
- Azure Cosmos DB account (serverless, NoSQL API, partition key `/category`)
- Application Insights (linked to the Function App for observability)

The email variant additionally needs an Entra ID app registration with Microsoft Graph application permissions. See [`function-email-and-teams/README.md`](./function-email-and-teams/README.md).

## What you'll need to install locally

Nothing, if you only deploy to Azure. If you want to inspect or run anything locally:

- Python 3.11+
- Azure Functions Core Tools v4
- Azure CLI

The labs do not require local execution.