# Withdraw bot

Loads the **sent-invitation manager** back to a cutoff date and withdraws every
invitation older than it, oldest first — the pending-invite list is capped, and
months-old invitations that were never accepted are what fills it.

> See the [root README](../README.md) for the one-time setup (attaching Chrome,
> installing dependencies) and the conventions shared by the bots.

## What it does

Starting from `linkedin.com/mynetwork/invitation-manager/sent/` (it navigates
there if you're elsewhere), the bot:

0. **Finds the tab.** Attaching to a running Chrome lands on an arbitrary tab,
   so the bot switches to the invitation-manager tab (or navigates there)
   before touching anything — the same thing `connect` and `message` do.
1. **Expands the list to the very end.** The page is a LazyColumn: scrolling
   loads nothing, and every page after the first is appended by a **"Load
   more"** button. The bot clicks it repeatedly — through an in-page
   JavaScript loop by default, falling back to the trusted-click ladder in
   `common/clicking.py` if that stalls — until the button is gone and the
   oldest invitation is in the DOM. `--stop-early` stops at the cutoff
   instead, which is cheaper but never reaches the oldest.
2. **Walks upward from the oldest.** With the bottom of the list loaded,
   `targets_until` iterates backwards — oldest first, climbing towards today
   — and stops at the first invitation newer than `--until`. Same shape as
   the message bot's `-i` + `--date-limit` walk.

   Cards show relative ages (`Sent 3 months ago`), never dates. LinkedIn
   rounds down, so that label means *at least* three months: the parsed date
   is the **newest** the invitation can be. A card is withdrawn only when
   that newest-possible date is already past `--until`, so ambiguous cards —
   and any card whose age can't be parsed — are left alone (an unreadable age
   is stepped over, not treated as the boundary).
3. **Withdraws, oldest first.** Each withdrawal opens a confirmation dialog
   whose confirm button repeats the invitee's name
   (`aria-label="Withdraw invitation sent to Camilla Souza"`). The bot checks
   that name against the card it clicked and **dismisses the dialog instead
   of confirming** when they disagree — withdrawing the wrong person can't be
   undone, a skipped card can. It then waits for the card to leave the list
   before moving on, with a randomized 1.5–3 s gap between withdrawals. A
   dialog left open after a failure is cancelled, since a modal `<dialog>`
   blocks every click beneath it.

## Two ways down the list

The default is **expand, then walk up**: load every page, then withdraw from
the oldest card towards today. It is the thorough one — it always reaches the
true bottom of the list — but a thousand expanded cards make the page heavy,
and nothing is withdrawn until the loading finishes.

`--rolling` is the other way round: withdraw whatever is **already loaded** and
past `--until`, load two more pages, withdraw again, repeat until the list
ends. Withdrawn cards leave the DOM as new ones arrive, so the page stays about
the size it started at, and the run starts working immediately. It also starts
from whatever is on screen — scroll down to the first invitation past the
cutoff by hand, then run it, and it picks up from there.

Either way a card is withdrawn only when its own age proves it is past the
cutoff, and a failed withdrawal is never retried inside the same run.

## Usage

```bash
python main.py withdraw                              # load only, report how far back the list goes
python main.py withdraw --until 2026/05/01 --dry-run # preview — nothing is clicked
python main.py withdraw --until 2026/05/01           # withdraw, after a y/N confirmation
python main.py withdraw --until 1m --max 200 -y      # load 200 cards, withdraw the old ones among them
python main.py withdraw --until 1m --rolling         # withdraw as it loads, down to the oldest
```

| Flag | Meaning |
|---|---|
| `--until YYYY/MM/DD` | Withdraw invitations sent **on or before** this date. Omit to only load the list. |
| `--max N` | Load at most N invitations, then withdraw the ones among them older than `--until`. Keeps the page light. |
| `--rolling` | Withdraw what is loaded and past `--until`, load two more pages, repeat down to the oldest — instead of expanding the list first. |
| `--dry-run` | Log what would be withdrawn; click nothing. |
| `-y`, `--yes` | Skip the confirmation prompt. |
| `--max-pages N` | Cap the pages loaded — the same bound as `--max`, counted in page loads rather than cards. A page counts whether it came from clicking "Load more" or from the list paging on scroll. With `--rolling`, the pages loaded per top-up (default 2). `--max-clicks` is accepted as an alias. |
| `--no-js` | Drive every page with trusted clicks instead of the in-page loop. |
| `--stop-early` | Stop loading at the cutoff instead of expanding to the end. No effect with `--rolling`, which never expands up front. |
| `--probe` | Print which tab, selectors and buttons the bot can see, then exit. |
| `-l LEVEL` | Log verbosity (default `INFO`). |

Withdrawal is irreversible and the run is unattended once started, so `--until`
is required — there is no "withdraw everything" mode — and the first run asks
for confirmation unless `-y` is passed. Start with `--dry-run`.

## Notes

- **Withdrawing blocks re-inviting that person for up to 3 weeks** — LinkedIn
  says so in the confirmation dialog. Worth remembering before clearing
  invitations to recruiters you may want to reach again.
- **Withdrawing does not refund the weekly invitation limit.** It frees space in
  the *pending* list; the weekly counter the connect bot tracks is unaffected.
- `withdraw/examples/withdraw-dialog.html` is the captured confirmation dialog
  the selectors key off; keep it in sync when LinkedIn's markup shifts.
- **The whole list is heavy.** ~1000 invitations is ~100 pages; the fully
  expanded DOM slows the browser down, and the default now expands to the end
  so the upward walk has an oldest card to start from. `--stop-early` skips
  that cost when the oldest invitations aren't the ones you're after.
- Age parsing and target selection are pure functions (`parse_sent_age`,
  `targets_until`, `targets_older_than`) covered by `tests/test_sent_age.py`.
- **When a run finds nothing, run `--probe` first.** It prints the open tabs,
  how many cards each candidate selector matches, whether the withdraw links
  and the "Load more" button are visible, and the last few button labels on
  the page — which separates "wrong tab" from "markup changed" from "list
  never mounted" without guessing.
