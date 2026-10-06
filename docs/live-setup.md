# Acquiring live credentials

Live checks are pending because these credentials are not available yet. Keep all secret values in the ignored `.env` file or a secret manager.

## Telegram

1. Use a Telegram account to open [@BotFather](https://t.me/BotFather), run `/newbot`, choose its name and username, and put the resulting token in `TELEGRAM_BOT_TOKEN`.
2. Each approver opens the new bot in a **private chat** and sends `/start`.
3. Run `uv run python scripts/telegram_setup.py chat-ids`. Add the printed IDs to comma-separated `TELEGRAM_APPROVER_CHAT_IDS`.
4. Generate a random `TELEGRAM_WEBHOOK_SECRET`, configure an HTTPS `PUBLIC_ADMIN_URL` that reaches the admin service through a reverse proxy, then run `uv run python scripts/telegram_setup.py set-webhook`.
5. Restart the admin and notifier services to load the settings. Trigger an R2 action and approve it in Telegram.

The bot uses private chats so the callback sender can be checked against the approved chat ID. The HTTP admin port is for local development. [Telegram BotFather guide](https://core.telegram.org/bots/features#botfather), [Telegram webhook API](https://core.telegram.org/bots/api#setwebhook).

## OpenAI

You need an OpenAI API organization, permission to create an organization Admin API key, a dedicated project per client, and an ordinary project inference key. The Admin API key is for usage/cost reporting; the project key is encrypted through the gateway's provider-credential admin endpoint. Put the reporting key in `OPENAI_ADMIN_KEY` and the project ID in `OPENAI_PROJECT_ID`. A ChatGPT subscription does not provide these organization credentials. [Organization administration API](https://developers.openai.com/api/reference/resources/organization/).

## Anthropic

You need a Claude Console organization and an administrator role to create an Admin API key, plus a dedicated workspace and inference key per client. Set `ANTHROPIC_ADMIN_KEY` and `ANTHROPIC_WORKSPACE_ID`. Individual accounts do not support the Admin API. [Usage and cost API prerequisites](https://platform.claude.com/docs/en/manage-claude/usage-cost-api).

The provider accounts also need enough API access and credits for inference test calls. Exact prices depend on the selected models. Reporting credentials grant organization access; each client's dedicated project/workspace mapping determines its report scope.
