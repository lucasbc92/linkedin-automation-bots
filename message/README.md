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
3. Walks the conversation list **strictly in one direction, one card at a
   time** — downward (newer→older) by default, or upward (older→newer) with
   `-i`/`--inv`. The next contact is always the adjacent card in that
   direction, never a rescan from the top. For each card it:
   - skips **Sponsored**, **InMail**, and **LinkedIn Offer** cards (identified
     by their pill label);
   - skips anyone already recorded in the send history;
   - optionally stops once a conversation crosses `--date-limit` (older than
     it going down, newer than it going up with `-i`);
   - opens the thread, types a personalized message into the compose box, and
     sends it.
4. Lazy-loads more conversations (by focusing the last card) until it reaches
   the bottom of the list (or hits `--max`). No lazy-loading is needed walking
   upward with `-i` — every card above the start point is already loaded.

Sending a message makes that conversation jump to the top of the list, so the
bot captures the *next* card **before** sending. That is what keeps the walk
anchored: nothing after the send is ever computed relative to the top of the
list, so already-messaged contacts up there can never become targets again.

The message text comes from a template in [`message/msg_templates/`](../message/msg_templates/).
`{name}` is replaced with the contact's first name. Unlike the connect bot,
direct messages have **no 300-character cap**, so templates are never truncated.
See [Templates](../README.md#templates) in the root README for the format.

## Usage

```bash
python main.py message                        # default template, whole inbox
python main.py message --dry-run              # preview who/what — sends nothing
python main.py message --max 10               # stop after 10 messages
python main.py message --date-limit 2025/12/31  # stop at conversations older than this
python main.py message --start-date 2025/07/30  # scroll down to this date and start there
python main.py message -m reconnect.txt --max 5 --dry-run
python main.py message -i -m reconnect_older.txt --date-limit 2024/10/20
    # click an old conversation first, then walk upward (older→newer),
    # stopping once a conversation is newer than the date limit
python main.py message --last-message-regex ".*há 7.*" \
    --last-message-regex-custom ".*for 7.*" message_ingles.txt
    # Portuguese previews get message.txt, English ones message_ingles.txt
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-m`, `--message FILE` | `message.txt` | Template file in `message/msg_templates/`. A bare filename is resolved against that folder; a path is used as-is. A file that doesn't exist stops the run. |
| `--date-limit YYYY/MM/DD` | none | Stop when a conversation is older than this date (or, with `-i`/`--inv`, newer than this date). |
| `--start-date YYYY/MM/DD` | none | Scroll down the list (lazy-loading as needed), click the **first conversation dated on or before** this date, and start sending from there, inclusive. If no such conversation exists, the run ends without sending. |
| `-i`, `--inv` | off | Walk the list **upward** (older → newer) instead of downward. Also flips `--date-limit` to mean "stop once a conversation is newer than this date." Useful for working through very old contacts from the bottom of the inbox up. |
| `--dry-run` | off | Log who would be messaged and the first line of the text, without opening threads or sending anything. |
| `--max N` | unlimited | Stop after sending `N` messages (a blast-radius cap). |
| `--last-message-regex REGEX` | none | Only message conversations whose last-message preview matches `REGEX`; they get the `-m` template. See [Routing by last message](#routing-by-last-message). |
| `--last-message-regex-custom REGEX FILE` | none | Send `FILE` instead of the `-m` template to conversations matching `REGEX`. Repeatable. |
| `-l`, `--log-level` | `DEBUG` | `DEBUG`, `INFO`, `WARN`, or `ERROR`. |

## Routing by last message

Each conversation card shows a preview of its last message, and that preview
can decide **whether** a card gets messaged and **which** template it gets.

- `--last-message-regex REGEX` — only cards whose preview matches are messaged,
  using the `-m` template (default `message.txt`).
- `--last-message-regex-custom REGEX FILE` — cards matching `REGEX` get `FILE`
  instead. Pass it as many times as you have templates.

```bash
python main.py message --last-message-regex ".*há 7.*" \
    --last-message-regex-custom ".*for 7.*" message_ingles.txt
```

That run messages a contact whose preview says "há 7 anos" with `message.txt`,
one whose preview says "for 7 years" with `message_ingles.txt`, and skips
everyone else — a way to answer in whichever language the thread is already in.

Rules are evaluated **in order: the custom ones first (in the order given),
then `--last-message-regex` as the fallback**, and the first match wins. So a
broad `--last-message-regex` can never steal a card from a specific custom
rule; among custom rules, put the most specific pattern first.

Used **alone**, `--last-message-regex-custom` still filters: cards matching
none of the patterns are skipped. With neither flag, every card gets the `-m`
template as before.

Both flags take Python regular expressions, matched with `re.search` against
the preview text (so no need to anchor them). Every pattern is compiled and
every template file checked at startup — a bad regex or a filename typo stops
the run before the first thread is opened, rather than silently falling back to
generic text. Templates are resolved against `message/msg_templates/` unless
you give a path.

A card whose preview LinkedIn hasn't rendered (occluded cards are emptied by
its virtualized list) matches nothing and is skipped, so `--dry-run` is worth a
pass before a real run: it logs each contact with the template that would be
used.

## Where it starts

- **No conversation open** → starts from the top of the list (most recent), or
  the bottom (oldest) when `-i`/`--inv` is given.
- **A conversation already open** (you clicked one before launching) → starts
  **from that conversation, inclusive**, and continues in the walk direction
  (downward by default, upward with `-i`). Every card on the other side of the
  active one is skipped. This lets you resume from a known point, e.g. click
  the oldest conversation in your inbox and run with `-i` to work upward
  through old contacts.
- **`--start-date` given** → the bot scrolls the list itself (focusing the last
  card to trigger lazy-loading, since plain scrolling doesn't reliably load
  more) until it finds the first conversation dated on or before that date,
  clicks it, and starts from there — same as if you had clicked it manually.
  This works the same way regardless of `-i`, since it only affects where the
  walk *starts*, not which direction it goes afterward.

Every contact a message is confirmed sent to is also recorded in
`message/.sent_history.json` (gitignored). The conversation list re-sorts by
recent activity — a reply, or any other conversation getting activity — so it
isn't append-only between runs. The history is loaded on startup and used as a
skip check while walking, so someone already messaged in an earlier run is
never re-messaged even if they end up *below* wherever you resume. It is only
ever used to *skip* a card — navigation itself is purely "the next card down".

## Date parsing

The `--date-limit` and `--start-date` checks rely on parsing LinkedIn's
conversation-card timestamps, which come in several formats. `parse_card_timestamp()` in
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
