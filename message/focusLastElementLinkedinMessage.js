function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function focusLastElement() {
    const ul = document.querySelector('.list-style-none.msg-conversations-container__conversations-list');

    if (!ul) {
        console.error('[focusLastElement] Container ".msg-conversations-container__conversations-list" not found.');
        return;
    }

    let oldLength = -1; // force at least one iteration
    let newLength = ul.children.length;

    console.log(`[focusLastElement] Starting. Initial item count: ${newLength}`);

    while (oldLength !== newLength) {
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

focusLastElement();