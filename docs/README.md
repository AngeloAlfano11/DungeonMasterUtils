# Inventory & Notes Mini App

Static frontend for the DungeonMasterUtils bot's inventory + notes feature.
Runs as a Telegram Mini App, stores data in `Telegram.WebApp.CloudStorage`
(per-user, per-bot, synced across devices). No backend.

## Files

- `index.html` — entry point and DOM structure
- `app.js` — storage abstraction + UI logic
- `style.css` — minimal styles, picks up Telegram theme variables

## Hosting (GitHub Pages)

1. Push this `miniapp/` folder somewhere GitHub can serve from. Either:
   - Whole repo, then set **Settings → Pages → Source = main branch /miniapp**
   - Or a dedicated repo with `index.html` at the root.
2. Wait a minute for Pages to provision; you'll get an HTTPS URL like
   `https://<your-username>.github.io/<repo>/`.
3. Open that URL in a desktop browser to confirm the page renders. In a
   browser without Telegram, CloudStorage isn't available, so the app
   transparently falls back to `localStorage` (just for dev preview — data
   isn't synced).

## Wiring to the bot (BotFather)

1. Open chat with `@BotFather`, `/mybots`, select your bot.
2. **Bot Settings → Configure Mini App** → paste the GitHub Pages URL.
3. **Bot Settings → Menu Button** → set:
   - Text: `🎒Inventory📝`
   - URL: same GitHub Pages URL
4. Reopen the chat with the bot from any client — the menu button appears
   under the message composer.

## Data layout in CloudStorage

| Pattern                | Type   | Content                                      |
|------------------------|--------|----------------------------------------------|
| `item_<uuid6>`         | JSON   | `{name, qty, notes}` — one key per item      |
| `note_<slug>_0`        | JSON   | `{title, content}` — header chunk of a note  |
| `note_<slug>_<n>` (n≥1)| string | continuation chunks, raw text                |

Constraints:
- Up to 1024 keys per user (Telegram limit).
- Up to 4096 chars per key value.
- Per-item notes are capped to 1000 chars (UI `maxlength`).
- Per-note content is capped to ~14800 chars total, split across 4 chunks.

## Development outside Telegram

Just open `index.html` in a browser. The app uses `localStorage` as a
fallback when `Telegram.WebApp.CloudStorage` isn't reachable. Useful for
laying out the UI without round-tripping through Telegram.

## Things deliberately out of scope (for now)

- Server-side visibility (the Python bot doesn't see inventories).
- Cross-user sharing (one player can't see another's items).
- Bot commands like `/inv` (text-based equivalents).

Adding any of the above requires a backend HTTP service exposing validated
endpoints. See the project plan for the hybrid architecture.
