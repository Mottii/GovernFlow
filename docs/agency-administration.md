# Agency administration and review routing

Phase 1 adds agency-bound memberships, several client assignments per user, reviewer groups, scoped integration destinations, and agency price overrides. Migrations `0015`–`0018` upgrade existing installations without replacing client, workflow, user, key, password, or session identifiers.

## Database identities and upgrades

Set a generated, URL-safe `APP_DATABASE_PASSWORD` of at least 20 characters in your private environment. Keep the existing `POSTGRES_PASSWORD`, encryption key, and signing key. `docker compose up -d --build` runs the one-shot `initialize` service before starting the gateway, ordinary admin API, and workers. Initialization applies migrations, seeds shared prices, and provisions the separate `governflow_app` login. Applications refuse an owner, superuser, role-administrator, or bypass-RLS login. `ENFORCE_AGENCY_RLS=false` is reserved for metadata-created test databases.

For installations outside Compose, apply migrations using a separate operator connection and provision a login with only `governflow_runtime` membership. Point all ordinary services and workers at that restricted login. Every transaction derives its agency context from an authenticated session, workflow key, or verified integration credential. Headers and request parameters cannot grant access to another agency.

The optional Compose `maintenance` profile runs an operator API at `127.0.0.1:8003`, using the owner connection and `MAINTENANCE_MODE=true`. Start it for agency provisioning and shared catalog maintenance; stop it afterward. Keep its token private. Ordinary agency administrators use port 8001 and cannot write the shared catalog. The existing bootstrap token still supports first-admin creation in the default agency; restricting bootstrap to initialization is scheduled in Phase 7.

## Reviewer setup

Use an authenticated agency administrator session for these APIs. Users also need active agency memberships and client access; a group assignment alone does not grant client access.

1. `POST /admin/reviewer-groups` with `name`, `client_id`, and `user_ids` creates a group. Members must have an active `admin` or `approver` membership. `PUT /admin/reviewer-groups/{id}` replaces its name, members, and active state, keeping its client fixed.
2. `PUT /admin/reviewer-groups/{id}/assignments` with `{"workflow_ids":["workflow-uuid"]}` assigns specific workflows. `[null]` assigns all workflows of that client. `[]` removes all assignments. Workflows must belong to that group’s client.
3. `POST /admin/integration-destinations` creates an explicit destination. Supply `name`, `channel`, `client_id`, optional `workflow_id`, and `enabled`. Telegram/email destinations also require `group_id`. `PUT /admin/integration-destinations/{id}` updates configuration while keeping an assigned client fixed. Create a new destination and credential to serve another client. GET endpoints list groups and destinations without secrets.

An example email destination body:

```json
{
  "name": "Invoice reviewers",
  "channel": "email",
  "client_id": "client-uuid",
  "workflow_id": "workflow-uuid",
  "group_id": "group-uuid",
  "enabled": true
}
```

Email recipients come from the currently authorized group members’ user accounts. Configure SMTP transport and `PUBLIC_DASHBOARD_URL`; global recipient settings are no longer routing authority. Review GET and POST require a current signed-in administrator or assigned approver. Forwarding a link does not transfer permission. Agency administrators retain scoped dashboard recovery authority during migration.

Human delivery and review recheck current membership, client access, group activity, and workflow assignments. Telegram and webhook callbacks also require a matching enabled destination; email review uses the authenticated dashboard permissions. Removing a reviewer or assignment invalidates links and Telegram callbacks already delivered. Permission mutations serialize with decision authorization within the agency.

## Telegram account linking

Configure `TELEGRAM_BOT_TOKEN` on the notifier and `TELEGRAM_WEBHOOK_SECRET` on the callback API. A signed-in user calls `POST /admin/telegram/link` to receive a personal `/link ...` command, valid for five minutes and one use. Send it to the bot in a private chat. The bot binds the Telegram account to that user; it cannot claim an account linked to someone else. `DELETE /admin/telegram/link` unlinks the caller’s account.

Create a Telegram destination for the reviewer group. Notifications go only to linked, currently authorized users. Callbacks require the transport secret, a valid chat-bound signature, the linked account, and a current matching destination and assignment. Global chat IDs have no delivery or decision authority.

## Webhook service identities

Create a destination with `channel: "webhook"`, a public HTTPS `url`, `service_identity`, and explicit client/optional workflow scope. The creation response returns a one-time `credential`; GET responses never reveal it. Store it privately. Each destination has a separate encrypted secret.

Outgoing notification bodies are signed with HMAC-SHA256 using that destination credential. To decide an action, POST JSON to `/integrations/webhook/{destination_id}/decision` with:

- `Authorization: Bearer <destination credential>`
- `x-gas-timestamp`: current Unix seconds
- `x-gas-signature`: hexadecimal HMAC-SHA256 of `timestamp + "." + raw_body`
- Body: `event_id` (fresh UUID), `approval_id`, and `decision` (`approve` or `deny`).

Signatures expire after five minutes. Reused event IDs are rejected. The destination must still be enabled and match the action’s client/workflow. Audit decisions record `webhook:<destination UUID>:<configured service identity>`; submitted `approver` values are ignored. The former global `/integrations/webhook/decision` endpoint returns 410.

## Legacy notification migration

Provide the existing `TELEGRAM_APPROVER_CHAT_IDS`, `APPROVAL_EMAIL_TO`, and `APPROVAL_WEBHOOK_URL` to the initializer during upgrade. Each configured setting is imported once as a disabled destination in the default agency; its original value is encrypted. No legacy global route is enabled automatically. Assign a client, group/workflow scope, and explicit current configuration before enabling a destination. Legacy pending deliveries are cancelled to prevent stale global routing. Existing approvers’ client grants are backfilled into explicit reviewer groups so dashboard review continues.

## Agency price overrides

`POST /admin/agency-prices` accepts the same validated price fields as the shared `/admin/prices` API and creates an effective-dated override for the authenticated agency. `GET /admin/agency-prices` lists that agency’s overrides. `DELETE /admin/agency-prices/{provider}/{model}/{effective_from}` removes one override. Gateway pricing selects the newest applicable agency override, falling back to the shared catalog. Custom OpenAI model names also appear in `/v1/models`, subject to workflow policy. Only platform maintenance can modify the shared catalog.

## Acceptance boundary

The Phase 1 suite uses Postgres 16 and a real restricted login, with two agencies and two clients each. It covers RLS reads/writes, parent ownership, authentication, native settlements, scoped notifications, forwarded links, Telegram linking and wrong identities, reviewer removal, webhook scope/identity, price overrides, transaction reuse, and populated upgrade preservation. Delivery transports are mocked. Live provider/channel verification remains Phase 8; hosted deployment remains Phase 7.
