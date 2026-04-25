# Telegram Reseller Shop Bot

A complete reseller shop bot for Telegram with:

- Phone-login user onboarding with admin approval flow
- Wallet, transactions, and per-user custom pricing
- Dynamic Platform → Panel → Plan → Key shop with atomic key delivery
- Orders, statistics, and full admin panel (products, keys, users, balance, broadcast, files, search, settings)
- Automatic Telegram backup of `shop.db`
- Built-in HTTP keep-alive endpoint for free Render hosting + UptimeRobot

## Hosting
See [HOSTING_GUIDE.md](HOSTING_GUIDE.md) for full Render + UptimeRobot setup.

## Required environment variables
| Var | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_ID_1` | Primary admin Telegram numeric ID |
| `ADMIN_ID_2` | Optional second admin ID |
| `ADMIN_USERNAME_1` | Primary admin @username (no @) |
| `ADMIN_USERNAME_2` | Optional second admin @username |
| `SESSION_SECRET` | Any long random string |
| `DATABASE_URL` | (Optional) Postgres URL for Neon hosting |
| `BACKUP_CHAT_ID` | (Optional) Telegram chat ID where DB is auto-backed-up |

## Run locally
```bash
pip install -r requirements.txt
python bot.py
```
