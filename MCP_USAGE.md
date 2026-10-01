# SignalHub MCP v1

## Purpose

SignalHub MCP v1 gives an authenticated ChatGPT or agent a bounded, read-only view of
SignalHub operations, signals, opportunities, review queues, source health and automation.
It is intended for investigation and prioritisation; it cannot perform administrative actions.

## Connection

- Transport: MCP Streamable HTTP (JSON-RPC 2.0)
- Production URL: use the Terraform `mcp_url` output (`.../mcp`)
- Authentication: OAuth 2.1 bearer token with scope `signalhub-mcp/read`
- Human/ChatGPT flow: ChatGPT CIMD (`https://chatgpt.com/oauth/client.json`) through the
  SignalHub OAuth facade, then Cognito authorization code with PKCE; only members of the existing
  `NurserySignalAdmins` group are accepted
- Service-agent flow: Cognito client credentials held in the Secrets Manager secret from the
  `mcp_readonly_secret_arn` output
- Protected-resource discovery:
  `/.well-known/oauth-protected-resource/mcp`
- OAuth authorization-server discovery: `/.well-known/oauth-authorization-server`
- Client identification: CIMD with public-client token authentication method `none`
- Redirect URI: `https://chatgpt.com/connector_platform_oauth_redirect`
- The facade validates ChatGPT's live CIMD document, adds RFC 9207 `iss` to successful and error
  callbacks, and forwards the exact MCP `resource` through Cognito's RFC 8707 binding.

The authenticated capability document is at `/mcp/capabilities`. Never put OAuth client secrets,
access tokens or refresh tokens in source control, chat messages, screenshots or client-side code.

Example connector values (placeholders only):

```text
Server URL: https://example.execute-api.eu-west-1.amazonaws.com/mcp
Authentication: OAuth
Authorization URL: <Terraform mcp_oauth_authorization_url output>
Token URL: <Terraform mcp_oauth_token_url output>
Client identification: CIMD (discovered automatically)
Scopes: signalhub-mcp/read
PKCE: S256
```

In ChatGPT, enable developer mode, create a custom app, choose Streamable HTTP, and provide only the
production MCP URL. ChatGPT discovers the OAuth endpoints and stable CIMD client automatically,
then redirects to Cognito login. No client secret, pasted bearer token, predefined client ID or
connection-specific callback is required. The OAuth `resource` value is bound to the MCP URL and
must be present as the access-token audience.

The service client exists for controlled non-interactive agents and production smoke tests. Obtain
its short-lived access token through the authorised operational process; do not copy its persistent
client secret into an agent configuration.

The manual `MCP production smoke` GitHub Actions workflow uses OIDC to mint such a token, exercises
all tools with bounded calls, reports counts/latencies only, and never prints the credential.

## Tool catalogue

- `get_operations_summary` — system-wide operational health and backlogs.
- `search_signals` / `get_signal` — bounded signal search and redacted evidence detail.
- `search_opportunities` / `get_opportunity` — bounded opportunity search and explainable state.
- `get_needs_attention` — authoritative opportunity-quality exception queue.
- `get_review_backlog` — pending reviews, QA, matching and unmatched strong signals.
- `get_source_status` — persisted collector/source execution status.
- `get_automation_status` — watcher, publication and withdrawal runtime health.
- `get_recent_changes` — bounded persisted change timeline.

List tools default to 20 records and never exceed 100. Continue with the returned opaque cursor.

## Example workflows

- “How is SignalHub doing? Highlight failed automation and growing queues.”
- “List the top Needs Attention categories, then inspect three representative opportunities.”
- “Why is opportunity `<id>` not automatically published?”
- “Show CareProspect publication QA holdouts.”
- “Show approved unmatched signals that would create an opportunity.”
- “What lifecycle, publication or withdrawal changes occurred in the last 24 hours?”

## Security model

The MCP runs in a dedicated Lambda with a dedicated IAM role. Its OAuth clients have only the
`signalhub-mcp/read` scope. It has no provider credentials, queue-send permissions,
collector controls or mutation tools. Results omit S3 keys, raw evidence payloads, credentials and
full residential address projections. Calls are rate-limited and recorded in a dedicated MCP audit
table using bounded argument metadata; OAuth tokens and evidence payloads are never logged.

MCP v1 is strictly read-only. Its only database writes are operational request-audit records, never
signals, reviews, opportunities, lifecycle, publication, withdrawal or provider state.

## Future mutation-tool roadmap

Mutation tools are intentionally excluded. They should be considered only after sustained use of
v1 identifies repetitive actions with narrow preconditions, explicit confirmation, independent
authorisation scopes, bounded execution, idempotency and complete audit history.
