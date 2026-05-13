# function-email-and-teams

The Smart Support Ticket pipeline wired to a live Microsoft 365 mailbox via Microsoft Graph, with critical-ticket alerts routed to a Teams channel. This variant powers the Course 1 video walkthrough.

## What it does

Four functions are registered:

| Function | Trigger | Role |
|---|---|---|
| `process_support_ticket` | Blob (`.txt` in `support-tickets`) | Core AI classification pipeline |
| `subscription_manager` | Timer (every 30 min) | Creates/renews the Graph mail subscription |
| `webhook_receiver` | HTTP POST | Receives Graph notifications, writes `.txt` + `.meta.json` to blob storage |
| `reply_sender` | Cosmos DB change feed | Sends Graph replies or posts Teams alerts |

End-to-end flow:

```
M365 inbox  →  Graph notification  →  webhook_receiver  →  Blob Storage
                                                              ↓
                                                    process_support_ticket
                                                              ↓
                                                          Cosmos DB
                                                              ↓
                                                        reply_sender
                                                         ↓         ↓
                                                  Graph reply   Teams alert
```

## Required App Settings

Set these under **Function App → Settings → Environment Variables → App Settings**.

### Core pipeline (same as the simulated variant)

| Setting | Description | Where to find it |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | Foundry resource endpoint | Foundry portal → resource overview |
| `AZURE_OPENAI_API_KEY` | API key for the Foundry resource | Foundry portal → Keys and Endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | Name of your gpt-5-mini deployment | Foundry portal → Deployments |
| `COSMOS_ENDPOINT` | Cosmos account URI | Cosmos portal → Keys |
| `COSMOS_KEY` | Cosmos primary key | Cosmos portal → Keys |
| `COSMOS_DATABASE` | Database name | Whatever you named it |
| `COSMOS_CONTAINER` | Container name | Whatever you named it |
| `EMAILSTORAGE_CONNECTION` | Storage connection string | Storage account → Access keys |

### Cosmos change feed trigger

| Setting | Description | Notes |
|---|---|---|
| `COSMOS_CONNECTION_STRING` | Full Cosmos connection string | Required separately from `COSMOS_ENDPOINT` + `COSMOS_KEY` because the change feed binding expects a connection string, not endpoint + key. Both point to the same account. |

### Microsoft Graph integration

| Setting | Description | Where to find it |
|---|---|---|
| `GRAPH_TENANT_ID` | Entra tenant ID | Entra ID → Overview |
| `GRAPH_CLIENT_ID` | App registration client ID | App registration → Overview |
| `GRAPH_CLIENT_SECRET` | App registration client secret value | App registration → Certificates and secrets |
| `SUPPORT_MAILBOX_USER_ID` | Object ID of the support mailbox user | Entra ID → Users → support mailbox user → Overview |
| `SUPPORT_MAILBOX_ADDRESS` | Email address of the support mailbox (used to filter self-sent replies) | Same user as above |
| `WEBHOOK_URL` | Public HTTPS URL of `webhook_receiver`, including its function key | Function App → `webhook_receiver` → Get Function URL |
| `GRAPH_CLIENT_STATE` | Random string used to validate notifications | Generate any high-entropy string |

### Teams escalation (optional)

| Setting | Description | Notes |
|---|---|---|
| `TEAMS_WEBHOOK_URL` | Incoming webhook URL for the alerts channel | Configured via the Workflows app in Teams. The legacy Office 365 Connector incoming webhooks have been deprecated; new webhooks must use Workflows. |

If `TEAMS_WEBHOOK_URL` is unset, critical tickets are still suppressed from auto-reply but no Teams alert is posted.

## Required Azure setup

Beyond the core resources listed in the root README:

1. **App registration in Entra ID.**
   - Add **Microsoft Graph application permissions**: `Mail.Read`, `Mail.Send`.
   - Grant admin consent.
   - Create a client secret and copy the value into `GRAPH_CLIENT_SECRET`.
2. **`function-state` blob container.** The subscription manager persists the active subscription ID in `function-state/graph-subscription-id.txt` so it survives cold starts. The function creates the container on first write if missing — no manual setup required.
3. **`leases` container in Cosmos DB.** The change feed trigger needs a leases container. The binding has `create_lease_container_if_not_exists=True` set, so it provisions automatically on first run.
4. **Function App must be publicly reachable.** Graph delivers notifications to `WEBHOOK_URL` over the public internet. Flex Consumption Function Apps are publicly addressable by default. If you've added private endpoints or restricted inbound traffic, Graph won't be able to reach the webhook.

## How notifications work

The subscription manager runs every 30 minutes. On first run it creates a Graph mail subscription scoped to the support mailbox's Inbox folder, with an expiration ~2.9 days out (Graph's maximum). On subsequent runs it renews the existing subscription rather than creating a new one. The subscription ID is persisted in blob storage so the function can find it across restarts.

When mail arrives, Graph POSTs a notification to `WEBHOOK_URL`. The webhook handles three cases:

- **Validation handshake** — Graph sends `?validationToken=...` when first creating the subscription. The function echoes the token in plain text.
- **Lifecycle event** — e.g. `reauthorizationRequired`. The function patches the subscription to extend its expiration.
- **Change notification** — a new message. The function fetches the full message from Graph, strips HTML, and writes `{messageId}.txt` + `{messageId}.meta.json` to the `support-tickets` container.

The `.txt` blob triggers `process_support_ticket`. The `.meta.json` blob is filtered out by the function's `.txt`-only check.

## How replies work

`reply_sender` watches the Cosmos DB change feed. When a new ticket document appears, it inspects `priority` and `draft_response`:

- `priority == "critical"` → suppress reply, post a Teams alert (if `TEAMS_WEBHOOK_URL` is configured).
- No `draft_response` → skip (nothing to send).
- Otherwise → look up the original `messageId` from the `.meta.json` sidecar (keyed by the `source_blob` field on the Cosmos document), then POST to Graph `messages/{id}/reply`. Graph threads the reply onto the original conversation.

## Notes on the code

- **Graph delivers notifications at-least-once.** The webhook checks blob existence before writing and uses `overwrite=False` on the upload, so duplicate notifications for the same message don't produce duplicate pipeline runs.
- **The self-send filter.** Without `SUPPORT_MAILBOX_ADDRESS` set, the webhook can't tell when the support mailbox replies to itself, which can produce reply loops. Set this value explicitly.
- **HTML stripping is intentionally minimal.** The `_strip_html` helper handles common Outlook-generated patterns with regex. For production traffic, swap in `beautifulsoup4` or `html2text`.
- **Outbound deliverability.** Application-permission sends from Graph route through a different IP pool than interactive sends, with a lower sender reputation. Fine for internal M365 recipients (Course 1 demo); production scenarios sending to external recipients should evaluate SPF/DKIM and consider a dedicated send infrastructure.