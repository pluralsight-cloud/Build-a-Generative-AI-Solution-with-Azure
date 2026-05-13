# function-simulated-no-email

The Smart Support Ticket pipeline driven by manual blob uploads. This is the variant used in the Course 2 hands-on labs.

## What it does

One function is registered: `process_support_ticket`. It triggers on any new blob in the `support-tickets` container, sends the contents to Azure OpenAI for classification, and upserts a structured document to Cosmos DB.

```
Upload .txt  →  Blob trigger  →  Azure OpenAI (gpt-5-mini)  →  Cosmos DB
```

## Required App Settings

Set these under **Function App → Settings → Environment Variables → App Settings**.

| Setting | Description | Where to find it |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | Foundry resource endpoint, e.g. `https://your-resource.openai.azure.com` | Foundry portal → resource overview |
| `AZURE_OPENAI_API_KEY` | API key for the Foundry resource | Foundry portal → Keys and Endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | Name of your gpt-5-mini deployment | Foundry portal → Deployments |
| `COSMOS_ENDPOINT` | Cosmos account URI, e.g. `https://your-cosmos.documents.azure.com:443/` | Cosmos portal → Keys |
| `COSMOS_KEY` | Cosmos primary key | Cosmos portal → Keys |
| `COSMOS_DATABASE` | Database name, e.g. `SupportDB` | Whatever you named it |
| `COSMOS_CONTAINER` | Container name, e.g. `Tickets` | Whatever you named it |
| `EMAILSTORAGE_CONNECTION` | Connection string for the storage account hosting the `support-tickets` container | Storage account → Access keys |

## Required Azure setup

Before the function will work end-to-end:

1. **Storage account.** Create a blob container named `support-tickets`. The blob trigger reads from this exact name.
2. **Cosmos DB.** Create the database and container named above. Set the container's partition key to `/category`.
3. **Foundry resource.** Deploy a gpt-5-mini model. Note the deployment name — it goes in `AZURE_OPENAI_DEPLOYMENT`.
4. **Function App.** Deploy this code, then populate all eight app settings above.

## Triggering the pipeline

Upload a `.txt` file containing a support ticket to the `support-tickets` container. The blob trigger fires within seconds. Watch progress in Application Insights or the Function App's log stream.

Sample ticket format:

```
Subject: Can't log in
From: jane@example.com
---
I've tried resetting my password three times and I still can't get in.
Please help.
```

Any UTF-8 text body works — the format is a hint to the LLM, not a parsing requirement.

## Output

A document lands in Cosmos DB with the structure defined by the `TicketAnalysis` Pydantic model in `function_app.py`. Fields include `category`, `sentiment`, `severity`, `root_causes`, `draft_response`, and `is_critical`. Critical tickets get an additional `priority: "critical"` field and a `null` draft response.

## Notes on the code

- The function only processes `.txt` files. Other extensions are skipped.
- Cosmos writes use `upsert_item`, so re-running a blob (delete + re-upload, or Azure Functions retry) won't duplicate the document — the same blob name produces the same `id` only if you wire it that way. The current code uses a fresh UUID per execution, which means retries DO create new documents. If you need true idempotency, key the `id` off `blob_name`.
- The LLM call is wrapped in tenacity retry that fires only on 429 rate-limit errors. All other failures bubble up immediately.