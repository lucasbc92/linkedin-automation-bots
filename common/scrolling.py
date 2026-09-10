"""Driving LinkedIn's inner scroll container.

LinkedIn's app shell does not scroll the document. Measured on a live
people-search page: ``document.scrollingElement`` was 633px of content in a
633px viewport — not scrollable at all — while ``<main>`` held 1450px in a
524px viewport. ``window.scrollTo`` and ``window.scrollBy`` therefore move
nothing, and a virtualized list never mounts the rows below the fold no
matter how many times they are called.

Anything that needs a whole list in the DOM has to walk up from the list to
whichever ancestor actually scrolls and drive *its* ``scrollTop``. The
withdraw bot learned this first; the connect bot was still scrolling the
window, which is why it only ever saw the rows LinkedIn had already mounted.

Every script carries a ``linkedin-scroll:<verb>`` marker so tests can drive
these without matching on JavaScript source.
"""

import logging

logger = logging.getLogger("linkedin_bot")

# How far one step moves, as a fraction of the visible height. Slightly less
# than a full viewport so nothing is skipped over between two scans.
STEP_FRACTION = 0.85

_FIND_SCROLLER = (
    "const findScroller = (listSel) => {"
    "  let el = document.querySelector(listSel);"
    "  while (el && el !== document.body) {"
    "    const oy = getComputedStyle(el).overflowY;"
    "    if (/(auto|scroll|overlay)/.test(oy) && el.scrollHeight > el.clientHeight + 4)"
    "      return el;"
    "    el = el.parentElement;"
    "  }"
    "  return document.scrollingElement || document.documentElement;"
    "};"
)

# Reported after every move: how much of the list exists, where the scroller
# sits, and whether there is any list left below it.
_STATE = (
    "const state = (s, cardSel) => ({"
    "  cards: document.querySelectorAll(cardSel).length,"
    "  top: s ? Math.round(s.scrollTop) : 0,"
    "  height: s ? s.scrollHeight : 0,"
    "  view: s ? s.clientHeight : 0,"
    "  atBottom: s ? (s.scrollTop + s.clientHeight >= s.scrollHeight - 8) : true"
    "});"
)

_STATE_JS = ("/*linkedin-scroll:state*/" + _FIND_SCROLLER + _STATE +
             "return state(findScroller(arguments[0]), arguments[1]);")

_TOP_JS = ("/*linkedin-scroll:top*/" + _FIND_SCROLLER + _STATE +
           "const s = findScroller(arguments[0]);"
           "if (s) s.scrollTop = 0;"
           "return state(s, arguments[1]);")

_STEP_JS = ("/*linkedin-scroll:step*/" + _FIND_SCROLLER + _STATE +
            "const s = findScroller(arguments[0]);"
            "if (s) s.scrollTop = Math.min(s.scrollTop + s.clientHeight * arguments[2],"
            "                              s.scrollHeight);"
            "return state(s, arguments[1]);")

_EMPTY = {"cards": 0, "top": 0, "height": 0, "view": 0, "atBottom": True}


def _run(driver, script, list_selector, card_selector, *extra):
    try:
        state = driver.execute_script(script, list_selector, card_selector, *extra)
    except Exception as e:
        logger.debug(f"Scroll script failed: {type(e).__name__}: {e}")
        return dict(_EMPTY)
    return state if isinstance(state, dict) else dict(_EMPTY)


def list_state(driver, list_selector, card_selector):
    """Card count and scroll position of the list, without moving anything."""
    return _run(driver, _STATE_JS, list_selector, card_selector)


def scroll_list_to_top(driver, list_selector, card_selector):
    """Park the list's scroller at the top and report what is mounted."""
    return _run(driver, _TOP_JS, list_selector, card_selector)


def scroll_list_step(driver, list_selector, card_selector, fraction=STEP_FRACTION):
    """Scroll one step further down the list and report what is mounted."""
    return _run(driver, _STEP_JS, list_selector, card_selector, fraction)
