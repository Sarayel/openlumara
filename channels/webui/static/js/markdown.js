// =============================================================================
// Markdown Rendering
// =============================================================================

marked.setOptions({
    breaks: true,
    gfm: true
});

// Escape HTML
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function renderMarkdown(text) {
    // handle undefined or null safely
    if (!text) return '';

    // parse markdown
    const rendered = marked.parse(text);

    // and protect against XSS
    const clean = DOMPurify.sanitize(rendered);

    return clean;
}

function highlightCode(element, newOnly = false) {
    if (typeof hljs === 'undefined') return;

    const blocks = newOnly
    ? element.querySelectorAll('pre code:not([data-highlighted])')
    : element.querySelectorAll('pre code');

    blocks.forEach((block) => {
        hljs.highlightElement(block);
        block.setAttribute('data-highlighted', 'true');

        const pre = block.parentElement;
        pre.style.position = 'relative';
        pre.style.marginTop = '28px';

        // Add data attribute to identify code blocks
        pre.setAttribute('data-copyable', 'true');

        if (!pre.querySelector('.copy-btn')) {
            const btn = document.createElement('button');
            btn.className = 'copy-btn';
            btn.textContent = 'Copy';
            btn.setAttribute('aria-label', 'Copy code');
            btn.setAttribute('data-copy-btn', 'true');

            // Insert at top of pre
            pre.insertBefore(btn, pre.firstChild);
        }
    });
}

// Event delegation — handles clicks on ANY copy button, survives re-renders
document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy-btn]');
    if (!btn) return;

    const pre = btn.closest('[data-copyable]');
    if (!pre) return;

    const codeBlock = pre.querySelector('code');
    if (!codeBlock) return;

    navigator.clipboard.writeText(codeBlock.textContent).then(() => {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
            btn.textContent = 'Copy';
            btn.classList.remove('copied');
        }, 1500);
    });
});


