# Message bot

Walks your LinkedIn **Messaging** inbox (`linkedin.com/messaging`) newest-first
and sends a personalized follow-up to each existing conversation.

> See the [root README](../README.md) for the one-time setup (attaching Chrome,
> installing dependencies) and the conventions shared by both bots.

## What it does

Starting from an open **Messaging** tab, the bot:

1. Finds the messaging tab among your open Chrome tabs (falling back to the
   current tab).
2. Determines where to start (see [Where it starts](#where-it-starts) below).
3. Walks the conversation list top-to-bottom (newest-first). For each card it:
   - skips **Sponsored**, **InMail**, and **LinkedIn Offer** cards (identified
     by their pill label);
   - optionally stops once a conversation is older than `--date-limit`;
   - opens the thread, types a personalized message into the compose box, and
     sends it.
4. Lazy-loads more conversations by scrolling until it reaches the bottom of the
   list (or hits `--max`).

The message text comes from a template in [`templates/message/`](../templates/message/).
`{name}` is replaced with the contact's first name. Unlike the connect bot,
direct messages have **no 300-character cap**, so templates are never truncated.
See [Templates](../README.md#templates) in the root README for the format.

## Usage

```bash
python main.py message                        # default template, whole inbox
python main.py message --dry-run              # preview who/what — sends nothing
python main.py message --max 10               # stop after 10 messages
python main.py message --date-limit 2025/12/31  # stop at conversations older than this
python main.py message -m reconnect.txt --max 5 --dry-run
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-m`, `--message FILE` | `message.txt` | Template file in `templates/message/`. A bare filename is resolved against that folder; a path is used as-is. |
| `--date-limit YYYY/MM/DD` | none | Stop when a conversation is older than this date. Because the list is newest-first, the first too-old card halts the whole run. |
| `--dry-run` | off | Log who would be messaged and the first line of the text, without opening threads or sending anything. |
| `--max N` | unlimited | Stop after sending `N` messages (a blast-radius cap). |
| `-l`, `--log-level` | `DEBUG` | `DEBUG`, `INFO`, `WARN`, or `ERROR`. |

## Where it starts

- **No conversation open** → starts from the top of the list (most recent).
- **A conversation already open** (you clicked one before launching) → starts
  **from that conversation, inclusive**, and continues downward. Every card
  *above* the active one is skipped. This lets you resume from a known point or
  start partway down the inbox.

Every contact a message is confirmed sent to is also recorded in
`message/.sent_history.json` (gitignored). The conversation list re-sorts by
recent activity — a reply, or any other conversation getting activity — so it
isn't append-only between runs. This history is loaded on startup and merged
into the skip set, so someone already messaged in an earlier run is never
re-messaged even if they end up *below* wherever you click to resume.

## Date parsing

The `--date-limit` check relies on parsing LinkedIn's conversation-card
timestamps, which come in several formats. `parse_card_timestamp()` in
[`message/bot.py`](bot.py) handles:

| Card shows | Interpreted as |
|------------|----------------|
| `10:01 PM` / `9:12 AM` | today |
| `Mon`, `Tue`, … | most recent past occurrence (last 7 days) |
| `Jun 27` | that day, this year (or last year if it's in the future) |
| `Jun 27, 2024` | explicit day + year |
| `Mar 2024` | the 1st of that month |
| anything else | unknown → treated as within range (does **not** stop the run) |

## How it works (implementation notes)

These details matter if you need to adapt the bot to LinkedIn UI changes — the
selectors are grouped at the top of [`message/bot.py`](bot.py).

- **Send-on-Enter compose box.** LinkedIn's DM box is a `contenteditable` div
  that sends on Enter, so `send_keys` can't be used for multi-line text.
  `_insert_text()` injects the whole body (newlines as soft-breaks) in one
  atomic `execCommand('insertText')`, with an `innerText`-setter fallback.
- **Send + verification.** After inserting, the bot presses a trusted Enter; if
  the box doesn't clear it falls back to clicking the **Send** button. A send is
  considered successful only once the compose box is empty again.
- **Trusted clicks.** Conversation cards are opened with the same
  native → ActionChains → CDP → JS click ladder used by the connect bot.
- **Skip pills.** Sponsored ads, InMails, and LinkedIn Offers share the same
  pill component; the bot matches on the pill text
  (`sponsored` / `inmail` / `linkedin offer`) and skips those cards.

## Design docs

- [`message/messaging-bot.spec.md`](messaging-bot.spec.md) — the original spec
  (starting HTML and desired behavior).
- [`docs/superpowers/specs/2026-06-28-messaging-bot-design.md`](../docs/superpowers/specs/2026-06-28-messaging-bot-design.md)
  — the approved design that this bot implements.
