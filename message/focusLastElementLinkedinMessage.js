function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Accepts "2025/07/30" or "2025-07-30".
function parseMinDate(str) {
    const m = String(str).match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3]);
}

const MONTHS = {
    jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
    jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
};

const WEEKDAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

// LinkedIn card timestamps: "10:20 PM" (today), "Wed" (past week),
// "Jun 29" (current year) or "Jul 28, 2025" (older). Returns a Date at
// midnight, or null if the text doesn't match any known format.
function parseCardDate(text) {
    text = text.trim();
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    if (/^\d{1,2}:\d{2}\s*(AM|PM)$/i.test(text)) return today;

    const m = text.match(/^([A-Za-z]{3,})\s+(\d{1,2})(?:,\s*(\d{4}))?$/);
    if (m) {
        const month = MONTHS[m[1].slice(0, 3).toLowerCase()];
        if (month === undefined) return null;
        const day = +m[2];
        if (m[3]) return new Date(+m[3], month, day);
        let d = new Date(today.getFullYear(), month, day);
        // No year shown means current year, unless that lands in the future
        // (e.g. "Dec 30" seen in early January).
        if (d > today) d = new Date(today.getFullYear() - 1, month, day);
        return d;
    }

    const weekday = WEEKDAYS.indexOf(text.slice(0, 3).toLowerCase());
    if (weekday !== -1) {
        const diff = (today.getDay() - weekday + 7) % 7;
        return new Date(today.getFullYear(), today.getMonth(), today.getDate() - diff);
    }

    return null;
}

// Deepest (oldest) loaded card that has a parsable timestamp. Occluded
// cards are emptied by LinkedIn's virtualization and are skipped.
function deepestCardDate(ul) {
    const items = ul.children;
    for (let i = items.length - 1; i >= 0; i--) {
        const time = items[i].querySelector('time.msg-conversation-card__time-stamp');
        if (!time) continue;
        const date = parseCardDate(time.textContent);
        if (date) return { date, text: time.textContent.trim(), index: i };
    }
    return null;
}

// Scrolls the conversation list by focusing its last card until no more
// conversations load — or, if minDateStr ("yyyy/mm/dd") is given, until a
// loaded card's date is equal to or earlier than that date.
async function focusLastElement(minDateStr) {
    let minDate = null;
    if (minDateStr !== undefined) {
        minDate = parseMinDate(minDateStr);
        if (!minDate) {
            console.error(`[focusLastElement] Invalid min date "${minDateStr}". Use "yyyy/mm/dd", e.g. focusLastElement("2025/07/30").`);
            return;
        }
    }

    const ul = document.querySelector('.list-style-none.msg-conversations-container__conversations-list');

    if (!ul) {
        console.error('[focusLastElement] Container ".msg-conversations-container__conversations-list" not found.');
        return;
    }

    let oldLength = -1; // force at least one iteration
    let newLength = ul.children.length;

    console.log(`[focusLastElement] Starting. Initial item count: ${newLength}` +
        (minDate ? ` Will stop at dates on or before ${minDate.toDateString()}.` : ''));

    while (oldLength !== newLength) {
        if (minDate) {
            const deepest = deepestCardDate(ul);
            if (deepest && deepest.date <= minDate) {
                console.log(`[focusLastElement] Card at index ${deepest.index} is from "${deepest.text}" (${deepest.date.toDateString()}), on or before the min date. Stopping.`);
                return;
            }
        }

        oldLength = newLength;

        const targetIndex = ul.children.length - 2;
        const currentLastElement = ul.children[targetIndex];

        if (!currentLastElement) {
            console.warn(`[focusLastElement] No element found at index ${targetIndex}. Stopping.`);
            break;
        }

        console.log(`[focusLastElement] Focusing element at index ${targetIndex} (out of ${ul.children.length} total).`);
        currentLastElement.focus();

        console.log('[focusLastElement] Waiting 3s to let more conversations load...');
        await delay(3000);

        newLength = ul.children.length;
        console.log(`[focusLastElement] After wait -> oldLength: ${oldLength}, newLength: ${newLength}`);
    }

    console.log(`[focusLastElement] Done. Length stopped changing at ${newLength} items.`);
}

// Stop only at the end: focusLastElement();
focusLastElement("2025/07/30");

