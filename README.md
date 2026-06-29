# linkedin-automation-bots

A small set of Python + Selenium bots that automate LinkedIn outreach by driving
**your own already-logged-in Chrome session**. Two bots, one entry point:

| Bot | Command | What it does | Docs |
|-----|---------|--------------|------|
| **Connect** | `python main.py connect` | Walks people-search results and sends personalized connection invitations. | [connect/README.md](connect/README.md) |
| **Message** | `python main.py message` | Walks the Messaging inbox and sends a personalized follow-up to each conversation. | [message/README.md](message/README.md) |

Each bot has its own README with full option reference and implementation notes.
This page covers everything they share: setup, configuration, and conventions.

> **Heads-up.** Automating LinkedIn is against its Terms of Service and can get
> your account restricted. These tools throttle themselves with human-like
> delays and respect LinkedIn's limits, but you use them at your own risk. Keep
> volumes modest.

## How they work (the shared model)

Both bots **attach to an existing Chrome instance** over the DevTools protocol
rather than launching their own browser. That means:

- you log in to LinkedIn yourself, in a normal browser session, once; and
- the bots act as that session — no credential handling, no separate login.

You navigate to the right page (people-search results for `connect`, the
Messaging inbox for `message`), leave that tab open, and run the bot. It finds
the correct tab among your open tabs, then walks the page sending messages with
randomized pauses, keeping the PC awake until it finishes.

## Requirements

- **Python 3.10+** (developed on 3.14)
- **Google Chrome**
- Python packages in [`requirements.txt`](requirements.txt):
  - `selenium==4.33.0`
  - `argcomplete>=3.0` (shell tab-completion)

```bash
pip install -r requirements.txt
```

Selenium 4.33 has **Selenium Manager** built in, so a matching ChromeDriver is
downloaded automatically the first time you run a bot — no manual driver setup.

## Setup: attach Chrome to the bots

The bots connect to Chrome on `127.0.0.1:9222`, so Chrome must be started with
remote debugging enabled. **Fully quit Chrome first**, then relaunch it with the
flag:

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

**macOS:**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

**Linux:**
```bash
google-chrome --remote-debugging-port=9222
```

Then, in that Chrome window:

1. Log in to LinkedIn.
2. Open the page the bot needs:
   - **connect** → a people-search results page (`linkedin.com/search/results/people/…`)
   - **message** → the Messaging inbox (`linkedin.com/messaging`)
3. Run the bot from this project folder.

If no matching tab is found, each bot falls back to the current tab and warns
you.

## Running

```bash
python main.py connect [options]    # send connection requests
python main.py message [options]    # send follow-up messages
python main.py                      # prints help
python main.py connect --help       # per-command help + examples
```

See each bot's README for its full option list:
[connect](connect/README.md#options) · [message](message/README.md#options).

## Templates

Both bots read their message text from plain-text template files, so you can
edit wording without touching code.

```
templates/
├── connect/      # used by `python main.py connect`
│   ├── message.txt          (default)
│   ├── message_formal.txt
│   ├── message_objetiva.txt
│   └── … other variants
└── message/      # used by `python main.py message`
    ├── message.txt          (default)
    └── reconnect.txt
```

**Format** (handled by [`common/messages.py`](common/messages.py)):

- A file may hold **several variations separated by a line of three or more
  dashes** (`---`). The bot picks one at random per send, so your outreach
  doesn't look identical to everyone.
- `{name}` anywhere in the text is replaced with the contact's first name.
- If the contact's name can't be determined, `{name}` (and an adjacent comma /
  space) is dropped so the greeting still reads cleanly.

**Picking a template:** pass `-m <filename>` (resolved inside that bot's
`templates/<bot>/` folder), or `-m <path>` to point anywhere. With no `-m`, the
bot uses `message.txt`.

**Character limits:** connection-request notes are capped by LinkedIn at **300
characters** (counted as UTF-16 code units, so emoji = 2). The connect bot
enforces this — it drops `{name}` first, then hard-truncates if still too long.
Direct messages have no cap, so the message bot never truncates.

### First-name handling

`{name}` is filled with the contact's first name via
[`common/names.py`](common/names.py), which is tuned for **Brazilian names**: it
keeps compound first names like *João Victor*, *Ana Júlia*, and
*Maria de Lourdes*, while dropping surnames such as *Silva* or *Santos*.

## Shared behavior & configuration

These apply to both bots and are configured per-run via flags (there is no
config file):

- **Human-like delays.** Randomized pauses between actions and pages to avoid
  looking like a script.
- **Sleep prevention.** On Windows, the PC is kept awake during a run and
  restored afterward ([`common/sleep.py`](common/sleep.py)). A no-op on other
  platforms.
- **Logging.** Every run writes to **`last_run.log`** (overwritten each run) and
  echoes to the console. Control verbosity with `-l / --log-level`
  (`DEBUG` default, or `INFO` / `WARN` / `ERROR`). Configured in
  [`common/logging_setup.py`](common/logging_setup.py).
- **Run summary.** Each bot ends by printing a `sent / failed / skipped` tally.
- **Robust clicking.** LinkedIn ignores some synthetic clicks, so both bots use
  a native → ActionChains → Chrome DevTools Protocol → JavaScript click ladder
  to produce trusted events (including inside Shadow DOM).
- **Graceful stop.** `Ctrl+C` stops a run cleanly and still restores power
  settings.

## Shell tab-completion (optional)

`main.py` ships argcomplete hooks, including completion of template filenames for
`-m`. One-time setup:

**Bash / Git Bash** — add to `~/.bashrc`:
```bash
eval "$(register-python-argcomplete main.py)"
```

**Zsh:**
```zsh
autoload -U bashcompinit && bashcompinit
eval "$(register-python-argcomplete main.py)"
```

**PowerShell:**
```powershell
Register-ArgumentCompleter -Native -CommandName python -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $env:_ARGCOMPLETE = 1
    $env:COMP_LINE = $commandAst.ToString()
    $env:COMP_POINT = $cursorPosition
    python main.py 2>&1 | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
```

## Project layout

```
linkedin-automation-bots/
├── main.py              # CLI entry point + argument parsing
├── requirements.txt
├── common/              # shared across both bots
│   ├── browser.py       # attach to existing Chrome, perf-log/429 detection
│   ├── messages.py      # template loading, {name} substitution, length caps
│   ├── names.py         # Brazilian-aware first-name extraction
│   ├── sleep.py         # keep Windows awake during a run
│   └── logging_setup.py # file + console logging
├── connect/             # Connect bot  → see connect/README.md
│   ├── bot.py
│   └── examples/        # saved LinkedIn HTML fixtures for selector work
├── message/             # Message bot  → see message/README.md
│   ├── bot.py
│   └── messaging-bot.spec.md
├── templates/           # editable message text (see Templates above)
│   ├── connect/
│   └── message/
└── docs/                # design specs
```

## License

[MIT](LICENSE) © Lucas Bueno Cesario.
