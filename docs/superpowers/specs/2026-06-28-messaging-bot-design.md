# LinkedIn Messaging Bot — Design

**Date:** 2026-06-28
**Status:** Approved (brainstorming)
**Source spec:** `message/messaging-bot.spec.md`

## Context

`linkedin-automation-bots` is a monorepo with a working `connect` bot and a
scaffolded `message` package (`message/bot.py` is a placeholder; `main.py
message` only logs "not yet implemented"). This design fills in the messaging
bot described by the user's spec: a follow-up-message tool for the LinkedIn
**Messaging** page (`linkedin.com/messaging`).

**Goal:** walk the conversation list newest-first and send a personalized
message to each connection, mirroring the connect bot's operational style —
attach to the user's existing Chrome session, human-like delays, sleep
prevention, and a final run summary.

## Requirements & decisions

1. **Targeting** — send to **every non-Sponsored conversation**, top-to-bottom,
   until an optional date limit. No reply/unread filtering.
2. **Message source** — own `message/templates/` folder, same format as connect
   (variations separated by a `---` line, random pick, `{name}` → contact's
   first name). DMs have **no 300-char cap**, so no length truncation.
3. **Date limit** — `--date-limit YYYY/MM/DD`, optional. The list is
   newest-first, so the first card older than the limit **stops the whole run**.
   No flag → process the entire list.
4. **Guardrails** — `--dry-run`, random human-like delays, `--max N` cap, and
   skip-on-failure with a `sent / failed / skipped` summary.

## Architecture

### Shared module: `common/messages.py`

Extract the template/personalization logic currently embedded in
`LinkedInConnectBot` (`load_message_templates`, `personalize_message`,
`_message_length`, `_remove_name_placeholder`, `MESSAGE_SEPARATOR`,
`DEFAULT_MESSAGE`) into a reusable class:

```python
class MessageTemplates:
    def __init__(self, file_path, max_length=None, default=...): ...
    def personalize(self, name=None) -> str   # random variation + {name} fill
```

- `max_length=None` → no length checks/truncation (messaging).
- `max_length=300` → existing connect behavior (drop name / truncate to fit).
- `connect/bot.py` is refactored to consume this. The refactor is justified
  because the new bot needs the identical logic; it removes duplication rather
  than adding unrelated churn.

### New `message/bot.py`: `LinkedInMessageBot`

Reuses `common/browser.create_driver(attach_to_existing=True)`,
`common/names.display_first_name`, `common/sleep`, `common/logging_setup`, and
the robust-click pattern from connect. The Messaging compose form lives in the
**light DOM**, so no shadow-root handling is needed (unlike connect's invite
modal).

Key elements (from the spec HTML):

| Purpose | Selector |
| --- | --- |
| List container | `div.msg-conversations-container--inbox-shortcuts` |
| Conversation list | `ul.msg-conversations-container__conversations-list` → `li` |
| Participant name | `h3.msg-conversation-listitem__participant-names span.truncate` |
| Timestamp | `time.msg-conversation-listitem__time-stamp` |
| Skip marker | `span.msg-conversation-card__pill` (text "Sponsored", "InMail" or "LinkedIn Offer") |
| Compose box | `div.msg-form__contenteditable[contenteditable="true"][role="textbox"]` in `form.msg-form` |

## Core algorithm (`run`)

```
prevent_sleep(); select the messaging tab/url
templates = MessageTemplates("message/templates/message.txt")
processed = set()                      # participant names already handled
loop:
    cards = all <li> conversation cards currently in the list
    target = first card whose name not in processed AND not Sponsored/InMail/LinkedIn Offer
    if target is None:
        scroll list container to bottom to lazy-load more
        if card count did not grow -> break          # real bottom reached
        else continue
    name_full = participant name; first = display_first_name(name_full)
    date = parse_card_timestamp(card)
    if date_limit and date is not None and date <= date_limit:
        break                                          # stop entirely
    processed.add(name_full)
    if Sponsored: continue
    message = templates.personalize(first)
    if dry_run: log "[DRY-RUN] would send to {first}: {first line}"; continue
    robust_click(card); wait for thread to open
    verify active thread header name matches name_full  # reorder safety
    insert + send message; verify; update counters
    sleep(random 3-5s)
    if max and sent >= max: break
allow_sleep(); print summary (sent / failed / skipped)
```

**Reorder safety:** sending bumps a thread to the top of the list with a fresh
timestamp. Always selecting the *topmost unprocessed* card (processed-set keyed
by participant name) makes iteration robust — already-messaged threads sit in
`processed` and are skipped, and the date-limit check always evaluates the next
genuinely-older unprocessed card.

## Timestamp parsing (`parse_card_timestamp`)

LinkedIn uses relative formats. A tolerant parser, relative to today:

- `"10:01 PM"` / `"9:12 AM"` → **today**.
- weekday (`"Mon"`) → most recent past occurrence (within the last week).
- `"Jun 27"` (month + day) → that day in the current year; if it would be in
  the future, use the previous year.
- `"Jun 27, 2024"` / `"Mar 2024"` → explicit year.
- Unparseable → `None`; treated as **within range** (never triggers an early
  stop) and logged at WARN.

`--date-limit YYYY/MM/DD` is parsed to a `date`; comparison is date-granular.

## Message insertion + send

The compose target is a rich `contenteditable`, so the connect bot's
textarea-`value` setter does not apply, and `send_keys` of multi-line text
would send each line prematurely (Enter = send).

1. Focus / `robust_click` the `div.msg-form__contenteditable`.
2. Insert the full body (emoji + newlines) via
   `document.execCommand('insertText', false, text)` — preserves line breaks as
   soft breaks without firing the send handler. Fallback: set `innerText` and
   dispatch an `input` event.
3. Verify the box is non-empty (`textContent`).
4. Send with a single `element.send_keys(Keys.ENTER)` (matches the "Press Enter
   to Send" hint). **Fallback:** if the text is still present after a short
   wait, locate and click the form's Send button.
5. Verify sent: the compose box returns to empty / the placeholder reappears;
   otherwise count as failed and continue.

## CLI (`main.py message`)

Extend the existing `message` subparser:

- `-m, --message` — default `message/templates/message.txt`
- `--date-limit` — optional `YYYY/MM/DD`
- `--dry-run` — flag
- `--max` — optional int
- `-l, --log-level` — already present

`run_message` builds a banner (mirroring `run_connect`), constructs
`LinkedInMessageBot`, and calls `.run()`, with the same KeyboardInterrupt /
exception guards as connect.

## Files

- **New:** `common/messages.py`, `message/templates/message.txt` (starter copy).
- **Rewrite:** `message/bot.py` (placeholder today).
- **Edit:** `main.py` (flesh out `message` subcommand + `run_message`),
  `connect/bot.py` (consume `common/messages.MessageTemplates`).

## Verification

1. **No-browser unit tests:**
   - `MessageTemplates.personalize` — random pick, `{name}` fill, no-name
     fallback; connect path still drops/truncates at 300, message path does not.
   - `parse_card_timestamp` — table of inputs (`"10:01 PM"`, `"Jun 27"`,
     `"Mar 2024"`, weekday, garbage) → expected dates relative to a fixed today.
   - date-limit stop logic over a synthetic list of dates.
2. **Dry-run end-to-end:** Chrome started with `--remote-debugging-port=9222`,
   `linkedin.com/messaging` open, then `python main.py message --dry-run` —
   confirm correct people and order, Sponsored skipped, stop at `--date-limit`,
   and lazy-load on scroll.
3. **Live smoke test:** `python main.py message --max 1` against a known top
   thread; confirm one message is typed and sent, the list reorders, summary
   prints.

## Risks

- A synthetic `Enter` may not trigger LinkedIn's send in every build → the
  Send-button fallback covers this; validate during the live smoke test.
- Duplicate participant names would collide in the processed-set (rare). If it
  bites, extend the key with the avatar image `src`.
