# Connect bot

Walks your LinkedIn **people-search results** and sends personalized connection
invitations, page by page, until it runs out of results or hits LinkedIn's
weekly invitation limit.

> See the [root README](../README.md) for the one-time setup (attaching Chrome,
> installing dependencies) and the conventions shared by both bots.

## What it does

Starting from an open **people-search results** tab
(`linkedin.com/search/results/people/…`), the bot:

1. Finds the people-search tab among your open Chrome tabs (falling back to any
   search-results tab, then the current tab).
2. On each results page, scrolls to lazy-load every card, then walks every
   **"Invite … to connect"** control top-to-bottom.
3. Reads each person's headline off their card and skips anyone who is not a
   **tech** recruiter — see [Tech-recruiter filter](#tech-recruiter-filter).
   Skipped cards are never clicked, so they cost nothing against your weekly
   invitation quota.
4. For each remaining person, opens the invite modal and either:
   - clicks **Add a note**, types a personalized message, and clicks
     **Send invitation**; or
   - clicks **Send without a note** (when run with `-n`).
5. Verifies the invite registered (the Connect control turns **Pending**), then
   pages forward (or backward with `-r`) and repeats.

The message text comes from a template in [`connect/msg_templates/`](../connect/msg_templates/).
`{name}` is replaced with the contact's first name; see
[Templates](../README.md#templates) in the root README for the format.

## Usage

```bash
python main.py connect                       # default template, personalized note
python main.py connect -m message_formal.txt # pick a specific template
python main.py connect -n                    # send invitations with no note
python main.py connect -y -l INFO            # auto-continue past the warning, quieter
python main.py connect -r                    # page backwards (Previous instead of Next)
python main.py connect --max 80              # stop after 80 invitations sent
python main.py connect --any-title           # invite every recruiter, not just tech
python main.py connect --title-score 0.9     # stricter tech-recruiter filter
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-m`, `--message FILE` | `message.txt` | Template file in `connect/msg_templates/`. A bare filename is resolved against that folder; a path is used as-is. |
| `-n`, `--no-message` | off | Send invitations **without** a note (clicks "Send without a note"). |
| `-r`, `--reverse` | off | Navigate results in reverse (click **Previous** instead of **Next**). |
| `-y`, `--yes` | off | Auto-continue past the "close to the weekly invitation limit" warning instead of prompting. |
| `--max N` | unlimited | Stop after sending `N` invitations (blast-radius limit, independent of LinkedIn's own weekly cap). |
| `--any-title` | off | Invite **every** recruiter, skipping the tech-recruiter headline filter. |
| `--title-score S` | `0.80` | Minimum tech-recruiter similarity score (`0.0`–`1.0`). Raise it to be stricter, lower it to be more permissive. |
| `-l`, `--log-level` | `DEBUG` | `DEBUG`, `INFO`, `WARN`, or `ERROR`. |

## Tech-recruiter filter

A people-search for "recruiter" returns every flavour of recruiter — health,
legal, retail, sales — plus people who are not recruiters at all. By default the
bot invites only the ones hiring for **technology**, scoring each card's
headline with [`connect/tech_recruiter.py`](tech_recruiter.py) before clicking
anything.

Scoring is similarity-based (stdlib `difflib`), so it survives typos, casing,
accents and padding words. A headline scores through one of three routes:

| Route | Example | Score |
|-------|---------|-------|
| **Composite** — one phrase that proves it on its own | `Tech Recruiter \| Recrutamento e seleção` | 1.00 |
| **Co-occurrence** — a recruiting word and a technology word in the *same* segment | `Recrutamento de Desenvolvedores` | 0.90 |
| **Separate segments** — the same two words in different bullets | `Talent Acquisition Specialist \| Technology` | 0.85 |

Segments are the chunks between `|`, `/`, `•`, `,` and `;`, so a technology word
in an unrelated bullet cannot vouch for a recruiting word in another one.
Domain words that mark a *non*-tech specialisation (`saúde`, `jurídico`,
`varejo`, `engenharia civil`, …) subtract a penalty — enough to sink a
borderline match, never enough to sink a clean composite hit, since
`Tech Recruiter | Vendas` still recruits for tech.

Headlines below the threshold are logged with their score and reason and
counted separately in the run summary:

```
Skipping Ana — not a tech recruiter: 'Sales Recruiter' (score 0.00 < 0.80; ...)
Session summary — sent: 42 | failed: 0 | skipped: 11 (of which 9 not tech recruiters)
```

The word lists (`COMPOSITE_TERMS`, `RECRUITER_TERMS`, `TECH_TERMS`,
`NEGATIVE_TERMS`) live at the top of `connect/tech_recruiter.py` and are meant
to be edited. After changing them, re-run the tests:

```bash
python -m unittest discover -s tests -t .
```

To turn the filter off for one run, pass `--any-title`.

## Connection-request limits

LinkedIn caps invitations at roughly **100–200 per week**. The bot watches for
two kinds of limit signals and stops or prompts accordingly:

- **"Close to the weekly invitation limit"** — a warning. By default the bot
  prompts you (`Use remaining invites? (y/N)`); `-y` auto-continues.
- **"Reached the weekly invitation limit"** — a hard stop. The bot dismisses the
  dialog and ends the run.
- **HTTP 429** on LinkedIn's invitation endpoint — the quota can be exhausted
  with no on-screen dialog at all. When Chrome performance logging is available
  (see the root README), the bot detects this at the network level and stops.

When the limit is reached mid-run, the bot ends gracefully and prints the
session summary (`sent / failed / skipped`).

## Counting invitations

Every confirmed invitation is appended immediately to `connect/.invites.jsonl`
(gitignored — it holds real contact names). Because it's written the moment the
invite registers, the count survives a `Ctrl+C`, a crash, or a run at a log
level that hides INFO lines.

Every run — including one you interrupt — ends with the session summary plus
the running weekly total. To check the totals at any time:

```bash
python main.py stats             # invitations per week
python main.py stats --weeks 4   # last 4 weeks only
python main.py stats --backfill  # import past runs from connect/logs/ (idempotent)
```

A logging week runs Sunday 21:00 → Sunday 21:00 (America/São_Paulo), matching
the weekly log filenames.

## How it works (implementation notes)

These details matter if you need to adapt the bot to LinkedIn UI changes — the
selectors live in [`connect/bot.py`](bot.py).

- **Shadow DOM modal.** LinkedIn renders the invite modal inside an open Shadow
  DOM host (`#interop-outlet`). Selenium's XPath can't reach into shadow roots,
  so every modal interaction (Add a note, the textarea, Send) goes through
  `get_modal_shadow_root()` using **CSS selectors only**.
- **Trusted clicks.** LinkedIn ignores synthetic clicks for some controls, so
  `_robust_click()` tries, in order: native `.click()` → ActionChains → a
  Chrome DevTools Protocol mouse event (`isTrusted=true`, works inside shadow
  DOM) → a JavaScript `.click()` fallback.
- **Emoji-safe typing.** ChromeDriver's `send_keys` chokes on non-BMP characters
  (emoji). `fill_message_box()` types the plain part first to activate
  LinkedIn's input binding, then injects the full text via the native value
  setter and fires `input`/`change` events.
- **Name extraction.** The first name for `{name}` is pulled from the
  `Invite <Full Name> to connect` aria-label, with fallbacks that read the modal
  body and the surrounding profile card. Compound Brazilian first names are
  preserved (see [`common/names.py`](../common/names.py)).
- **"Enter their email to connect" screen.** Some profiles require an email to
  invite; the bot detects this, cancels, and skips the person.

### Reference HTML

[`connect/examples/`](examples/) holds saved LinkedIn HTML snippets (the invite
modal, note modal, email-input screen, follow-person card, old/new
search-results containers) used as fixtures when updating selectors.
