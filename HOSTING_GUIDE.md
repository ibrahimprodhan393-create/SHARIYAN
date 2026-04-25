# Host the Telegram Bot 24/7 — Render Free + UptimeRobot + Neon PostgreSQL

This guide covers everything:
- Deploying on Render's free Web Service plan
- Keeping it awake with UptimeRobot
- **Persisting all data in a free Neon PostgreSQL database** (recommended — survives every redeploy and restart)
- Optional fallback: Telegram-based `shop.db` backups when no `DATABASE_URL` is set

> **Recommended:** Set the `DATABASE_URL` env var to a Neon (or any PostgreSQL) connection string. The bot detects it automatically and stores everything in Postgres instead of the local `shop.db` file. With this set you can ignore the Telegram-backup section entirely — your data survives every Render redeploy.

---

## 1) Push the code to GitHub

The repo `https://github.com/asutoshkhanna354-del/telegram-shop-bot` already contains:
- `bot.py`, `requirements.txt`, `render.yaml`, `Procfile`
- `HOSTING_GUIDE.md` (this file)
- `neon_import.sql` (your data, ready to paste into Neon)

⚠️ Never commit `shop.db`, `.env`, or your bot token. The included `.gitignore` blocks them.

## 2) Deploy on Render

1. Go to https://render.com → **New + → Web Service** → connect the GitHub repo.
2. Render auto-detects `render.yaml`. Confirm: Runtime **Python**, Plan **Free**.
3. Click **Advanced → Environment Variables** and add:
   | Key | Value |
   | --- | --- |
   | `BOT_TOKEN` | from @BotFather |
   | `ADMIN_ID_1` | your Telegram numeric user ID |
   | `ADMIN_ID_2` | second admin ID (optional) |
   | `ADMIN_USERNAME_1` | your Telegram @username (no @) |
   | `ADMIN_USERNAME_2` | second admin @username (optional) |
   | `SESSION_SECRET` | any long random string |
   | `DATABASE_URL` | **(recommended)** PostgreSQL connection string from Neon/Supabase/Render PG. When set, all data is stored here and survives every redeploy. |
   | `BACKUP_CHAT_ID` | (only if you skip `DATABASE_URL`) — see step 4 below |
   | `BACKUP_INTERVAL_MIN` | `30` (default; lower = more frequent backups) |
4. Click **Create Web Service**. After deploy, open the Render URL — you should see `OK - bot alive`.

## 3) Keep it awake — UptimeRobot

1. Sign up at https://uptimerobot.com (free).
2. **+ Add New Monitor** → Type: **HTTP(s)**, URL: your Render URL, Interval: **5 minutes**. Save.

Render free sleeps after 15 min of no traffic — UptimeRobot pings keep it alive 24/7.

---

## 4) 🗄️ Auto-backup `shop.db` to Telegram (FREE persistence)

Render's free disk is ephemeral — every redeploy or restart wipes `shop.db`. The bot now backs itself up to a Telegram chat automatically. **Do this once and your data is safe forever.**

### Step 4a — Get a `BACKUP_CHAT_ID`
You have two options:

**Option A — Backup to your own DM** (simplest)
- Your `BACKUP_CHAT_ID` = your own Telegram numeric ID (same as `ADMIN_ID_1`).
- The bot must have spoken to you at least once (you've already pressed `/start`, so this is done).

**Option B — Backup to a private channel** (cleaner, recommended)
1. In Telegram, create a new **Private Channel** called e.g. "Shop Bot Backups".
2. Add your bot as an **Administrator** of that channel (give it "Post Messages" permission).
3. Forward any message from that channel to https://t.me/userinfobot — it replies with the channel ID (a negative number like `-1001234567890`).
4. Use that number as `BACKUP_CHAT_ID`.

### Step 4b — Set the env var on Render
- Render → your service → **Environment** → set `BACKUP_CHAT_ID` to the value from above.
- Click **Save Changes** — Render redeploys automatically.

### What you get
- Every 30 minutes the bot sends `shop-YYYYMMDD-HHMMSS.db` as a document to that chat with caption `🗄️ Auto backup`.
- You also have a manual **🗄️ Backup Database** button in the Admin Panel for one-tap backups any time.

### How to RESTORE after a Render restart wipes the DB
1. Open the backup chat in Telegram → find the most recent `.db` file.
2. **Forward it** (or send it) directly to your bot in DM.
3. The bot detects you're an admin and replies: `✅ Database restored from your file.`
4. Restart the bot from Render's dashboard (or wait for the next deploy) — your data is back.

---

## 5) 🐘 (Optional) Use Neon Postgres for true persistence

This is the "no backup needed" path. Neon gives you a free Postgres database that stays around forever.

### 5a — Create the Neon project
1. Go to https://neon.tech → sign up (free, no card).
2. Click **New Project** → name it `telegram-shop-bot` → region close to you → **Create**.
3. On the project page, click **Connection Details** → copy the **Connection string** (starts with `postgres://...`). Keep it secret.

### 5b — Import your existing data
1. In the Neon console, click **SQL Editor** (left sidebar).
2. Open `neon_import.sql` from this repo, copy its **entire contents**, paste into the editor.
3. Click **Run**. You should see "Success" and your tables get created and populated.

**SQL commands to verify (run these in the same editor):**
```sql
SELECT COUNT(*) AS users FROM users;
SELECT COUNT(*) AS panels FROM panels;
SELECT COUNT(*) AS plans FROM plans;
SELECT COUNT(*) AS keys FROM keys;
SELECT COUNT(*) AS orders FROM orders;
SELECT COUNT(*) AS transactions FROM transactions;
```

To re-import fresh (wipes everything first):
```sql
DROP TABLE IF EXISTS files, bot_settings, custom_pricing, transactions, orders, keys, plans, panels, users CASCADE;
```
Then re-run `neon_import.sql`.

### 5c — Connect the bot to Neon (next step)
The bot currently uses SQLite. Switching its live connection to Neon is a separate, careful refactor (about 50 SQL queries to adapt for Postgres syntax). Once that's done, you'd just add `DATABASE_URL` (your Neon connection string) to Render's env vars and the bot would use Neon instead of `shop.db`. Until then:
- You have your data safely imported into Neon (✅ done in 5b).
- The bot keeps running on SQLite + auto-backups to Telegram (works great on Render free).

---

## Updating the bot
Just push to GitHub → Render auto-redeploys (`autoDeploy: true` in `render.yaml`).

## Troubleshooting

| Issue | Fix |
| --- | --- |
| Render deploy fails | Check the Logs tab — usually a missing env var. |
| `OK - bot alive` works but `/start` doesn't reply | Verify `BOT_TOKEN`. Make sure no other process is polling the same bot. |
| Bot doesn't auto-backup | Check `BACKUP_CHAT_ID` is set; check Render logs for `Auto-backup scheduled every 30 min`. The bot must have spoken to that chat at least once. |
| Auto-backup says "Forbidden" | The bot isn't a member/admin of that chat. Add it. |
| Database keeps resetting | Either set up auto-backup (step 4) or move to Neon (step 5). Render free always wipes local files. |
