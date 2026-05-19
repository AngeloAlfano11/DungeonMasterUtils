/* Inventory & Notes mini-app.
 *
 * Single-file, no framework. Reads/writes Telegram CloudStorage (per-user,
 * per-bot, synced across devices). When run outside Telegram (e.g. desktop
 * browser during development) falls back to localStorage so the UI still
 * works for previewing.
 *
 * Key layout in CloudStorage:
 *   item_<uuid6>           → JSON {name, qty, notes}
 *   note_<slug>_0          → JSON {title, content}  (header chunk)
 *   note_<slug>_<n>        → raw string             (continuation chunks, n=1..3)
 */

// ============ Storage abstraction ============

const storage = (() => {
    const cs = window.Telegram?.WebApp?.CloudStorage;
    if (cs && typeof cs.getItem === 'function') {
        // Real Telegram CloudStorage. Wrap each callback-style call as a Promise.
        const wrap = (fn) => (...args) => new Promise((resolve, reject) => {
            fn(...args, (err, value) => err ? reject(err) : resolve(value));
        });
        return {
            getItem:     wrap(cs.getItem.bind(cs)),
            getItems:    wrap(cs.getItems.bind(cs)),
            setItem:     wrap(cs.setItem.bind(cs)),
            removeItem:  wrap(cs.removeItem.bind(cs)),
            removeItems: wrap(cs.removeItems.bind(cs)),
            getKeys:     wrap(cs.getKeys.bind(cs)),
        };
    }
    // Dev fallback: localStorage keyed by a prefix so it doesn't leak into other apps.
    console.warn('Telegram CloudStorage not available; using localStorage fallback (dev preview).');
    const PREFIX = 'dev_miniapp_';
    return {
        getItem:    async (k) => localStorage.getItem(PREFIX + k),
        getItems:   async (ks) => Object.fromEntries(ks.map(k => [k, localStorage.getItem(PREFIX + k)])),
        setItem:    async (k, v) => { localStorage.setItem(PREFIX + k, v); return true; },
        removeItem: async (k) => { localStorage.removeItem(PREFIX + k); return true; },
        removeItems: async (ks) => { ks.forEach(k => localStorage.removeItem(PREFIX + k)); return true; },
        getKeys:    async () => Object.keys(localStorage).filter(k => k.startsWith(PREFIX)).map(k => k.slice(PREFIX.length)),
    };
})();

// CloudStorage has no batch `setItems`; we just parallelise per-key setItem calls.
async function setMany(dict) {
    await Promise.all(Object.entries(dict).map(([k, v]) => storage.setItem(k, v)));
}

// ============ Utility ============

function uuid6() {
    const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let s = '';
    for (let i = 0; i < 6; i++) s += alphabet[Math.floor(Math.random() * alphabet.length)];
    return s;
}

function slugify(title) {
    return title
        .toLowerCase()
        .normalize('NFD')                     // separate accents from letters
        .replace(/[̀-ͯ]/g, '')      // strip combining marks
        .replace(/[^a-z0-9]+/g, '-')          // anything else → dash
        .replace(/^-+|-+$/g, '')              // trim leading/trailing dashes
        .substring(0, 32);                    // cap length
}

function splitIntoChunks(text, size) {
    if (!text) return [''];
    const chunks = [];
    for (let i = 0; i < text.length; i += size) {
        chunks.push(text.slice(i, i + size));
    }
    return chunks;
}

// ============ Inventory model ============

const ITEM_PREFIX = 'item_';

async function loadInventory() {
    const keys = await storage.getKeys();
    const itemKeys = keys.filter(k => k.startsWith(ITEM_PREFIX));
    if (itemKeys.length === 0) return [];
    const dict = await storage.getItems(itemKeys);
    const items = [];
    for (const key of itemKeys) {
        try {
            const obj = JSON.parse(dict[key]);
            items.push({
                id: key.slice(ITEM_PREFIX.length),
                name: obj.name || '',
                qty: typeof obj.qty === 'number' ? obj.qty : 0,
                notes: obj.notes || '',
            });
        } catch (e) {
            // Skip malformed entries silently; logging would be noise.
        }
    }
    // Alphabetical for predictable ordering.
    return items.sort((a, b) => a.name.localeCompare(b.name));
}

async function saveItem(id, item) {
    const actualId = id || uuid6();
    await storage.setItem(ITEM_PREFIX + actualId, JSON.stringify({
        name: item.name,
        qty: item.qty,
        notes: item.notes || '',
    }));
    return actualId;
}

async function deleteItem(id) {
    await storage.removeItem(ITEM_PREFIX + id);
}

// ============ Notes model ============

const NOTE_PREFIX = 'note_';
// Per-chunk content cap. Keep well under 4096 so the JSON wrapper of chunk 0
// (~30 chars + escaped title) always fits.
const MAX_CHUNK_CONTENT = 3700;
const MAX_CHUNKS = 4;
const MAX_NOTE_LENGTH = MAX_CHUNK_CONTENT * MAX_CHUNKS;

function chunkKeysOf(slug, allKeys) {
    const prefix = NOTE_PREFIX + slug + '_';
    return allKeys
        .filter(k => k.startsWith(prefix) && /^\d+$/.test(k.slice(prefix.length)))
        .sort((a, b) => parseInt(a.slice(prefix.length), 10) - parseInt(b.slice(prefix.length), 10));
}

async function listNotes() {
    const keys = await storage.getKeys();
    // Header chunks end with "_0".
    const headerKeys = keys.filter(k => k.startsWith(NOTE_PREFIX) && k.endsWith('_0'));
    if (headerKeys.length === 0) return [];
    const dict = await storage.getItems(headerKeys);
    const notes = [];
    for (const key of headerKeys) {
        try {
            const obj = JSON.parse(dict[key]);
            const slug = key.slice(NOTE_PREFIX.length, -2);  // strip "note_" and "_0"
            notes.push({ slug, title: obj.title || '(untitled)' });
        } catch (e) { /* skip malformed */ }
    }
    return notes.sort((a, b) => a.title.localeCompare(b.title));
}

async function loadNote(slug) {
    const allKeys = await storage.getKeys();
    const chunkKeys = chunkKeysOf(slug, allKeys);
    if (chunkKeys.length === 0) return null;
    const dict = await storage.getItems(chunkKeys);
    let title = '';
    let content = '';
    for (let i = 0; i < chunkKeys.length; i++) {
        const value = dict[chunkKeys[i]];
        if (value == null) continue;
        if (i === 0) {
            const obj = JSON.parse(value);
            title = obj.title || '';
            content = obj.content || '';
        } else {
            // Chunks 1+ are stored as raw strings (no JSON wrapper).
            content += value;
        }
    }
    return { slug, title, content };
}

async function uniqueSlug(title, currentSlug) {
    const base = slugify(title);
    if (!base) throw new Error('Title must contain letters or numbers.');
    const allKeys = await storage.getKeys();
    const headerSlugs = new Set();
    for (const k of allKeys) {
        if (k.startsWith(NOTE_PREFIX) && k.endsWith('_0')) {
            headerSlugs.add(k.slice(NOTE_PREFIX.length, -2));
        }
    }
    // Editing the same note: keeping its slug is always fine.
    if (currentSlug === base) return base;
    if (!headerSlugs.has(base)) return base;
    // Conflict: another note already owns `base`. Append a numeric suffix.
    let i = 2;
    while (headerSlugs.has(`${base}-${i}`)) i++;
    return `${base}-${i}`;
}

async function saveNote(oldSlug, title, content) {
    if (content && content.length > MAX_NOTE_LENGTH) {
        throw new Error(`Note too long: max ${MAX_NOTE_LENGTH} characters (currently ${content.length}).`);
    }

    const slug = await uniqueSlug(title, oldSlug);
    const chunks = splitIntoChunks(content || '', MAX_CHUNK_CONTENT);
    if (chunks.length > MAX_CHUNKS) {
        throw new Error(`Note too long: max ${MAX_CHUNKS} chunks (~${MAX_NOTE_LENGTH} chars).`);
    }

    // Compose new key/value pairs.
    const writes = {};
    writes[NOTE_PREFIX + slug + '_0'] = JSON.stringify({ title, content: chunks[0] });
    for (let i = 1; i < chunks.length; i++) {
        writes[NOTE_PREFIX + slug + '_' + i] = chunks[i];
    }

    // Write first, then clean up — so a failure mid-flight leaves the old
    // chunks intact (under their old slug) rather than wiping data and crashing.
    await setMany(writes);

    const allKeys = await storage.getKeys();
    const written = new Set(Object.keys(writes));
    const toDelete = [];
    for (const k of allKeys) {
        if (!k.startsWith(NOTE_PREFIX)) continue;
        if (written.has(k)) continue;
        // 1. The slug changed → drop the old slug's chunks entirely.
        if (oldSlug && oldSlug !== slug && k.startsWith(NOTE_PREFIX + oldSlug + '_')) {
            toDelete.push(k);
            continue;
        }
        // 2. Same slug but the new content has fewer chunks → drop stale tail.
        if (k.startsWith(NOTE_PREFIX + slug + '_')) {
            toDelete.push(k);
        }
    }
    if (toDelete.length > 0) {
        await storage.removeItems(toDelete);
    }

    return slug;
}

async function deleteNote(slug) {
    const allKeys = await storage.getKeys();
    const chunkKeys = chunkKeysOf(slug, allKeys);
    if (chunkKeys.length > 0) {
        await storage.removeItems(chunkKeys);
    }
}

// ============ UI: tab switcher ============

function showTab(name) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === name);
    });
    document.getElementById('view-inventory').hidden = (name !== 'inventory');
    document.getElementById('view-notes').hidden = (name !== 'notes');
}

// ============ UI: inventory ============

function renderInventoryList(items) {
    const ul = document.getElementById('inv-list');
    const empty = document.getElementById('inv-empty');
    ul.innerHTML = '';
    if (items.length === 0) {
        empty.hidden = false;
        return;
    }
    empty.hidden = true;
    for (const item of items) {
        const li = document.createElement('li');
        li.className = 'inv-item' + (item.qty === 0 ? ' depleted' : '');

        const row = document.createElement('div');
        row.className = 'inv-row';

        const nameEl = document.createElement('span');
        nameEl.className = 'inv-name';
        nameEl.textContent = item.name;

        const decBtn = document.createElement('button');
        decBtn.className = 'icon';
        decBtn.textContent = '−';
        decBtn.onclick = () => changeQty(item, -1);

        const qtyEl = document.createElement('span');
        qtyEl.className = 'inv-qty';
        qtyEl.textContent = String(item.qty);

        const incBtn = document.createElement('button');
        incBtn.className = 'icon';
        incBtn.textContent = '+';
        incBtn.onclick = () => changeQty(item, +1);

        const editBtn = document.createElement('button');
        editBtn.className = 'icon';
        editBtn.textContent = '✏️';
        editBtn.onclick = () => showInventoryForm(item);

        const delBtn = document.createElement('button');
        delBtn.className = 'icon';
        delBtn.textContent = '🗑️';
        delBtn.onclick = () => confirmDeleteItem(item);

        row.append(nameEl, decBtn, qtyEl, incBtn, editBtn, delBtn);
        li.appendChild(row);

        if (item.notes) {
            const notesEl = document.createElement('div');
            notesEl.className = 'inv-notes';
            notesEl.textContent = item.notes;
            li.appendChild(notesEl);
        }

        ul.appendChild(li);
    }
}

async function changeQty(item, delta) {
    const newQty = Math.max(0, item.qty + delta);
    if (newQty === item.qty) return;
    try {
        await saveItem(item.id, { name: item.name, qty: newQty, notes: item.notes });
        await refreshInventory();
    } catch (e) {
        alert('Save failed: ' + e.message);
    }
}

function showInventoryForm(item) {
    document.getElementById('inv-form').hidden = false;
    document.getElementById('inv-form-id').value = item ? item.id : '';
    document.getElementById('inv-form-name').value = item ? item.name : '';
    document.getElementById('inv-form-qty').value = item ? item.qty : 1;
    document.getElementById('inv-form-notes').value = item ? item.notes || '' : '';
    document.getElementById('inv-form-error').hidden = true;
    document.getElementById('inv-form-name').focus();
}

function hideInventoryForm() {
    document.getElementById('inv-form').hidden = true;
}

function confirmDeleteItem(item) {
    if (!confirm(`Delete "${item.name}"?`)) return;
    deleteItem(item.id)
        .then(refreshInventory)
        .catch(e => alert('Delete failed: ' + e.message));
}

async function refreshInventory() {
    try {
        const items = await loadInventory();
        renderInventoryList(items);
    } catch (e) {
        alert('Failed to load inventory: ' + e.message);
    }
}

// ============ UI: notes ============

function renderNotesList(notes) {
    const ul = document.getElementById('notes-list');
    const empty = document.getElementById('notes-empty');
    ul.innerHTML = '';
    if (notes.length === 0) {
        empty.hidden = false;
        return;
    }
    empty.hidden = true;
    for (const note of notes) {
        const li = document.createElement('li');
        li.className = 'note-item';
        li.textContent = note.title;
        li.onclick = () => openNoteEditor(note.slug);
        ul.appendChild(li);
    }
}

async function openNoteEditor(slug) {
    document.getElementById('notes-form').hidden = false;
    document.getElementById('notes-list').hidden = true;
    document.getElementById('notes-empty').hidden = true;
    document.getElementById('notes-add-btn').hidden = true;
    document.getElementById('notes-form-error').hidden = true;
    document.getElementById('notes-form-delete').hidden = !slug;

    if (slug) {
        const note = await loadNote(slug);
        document.getElementById('notes-form-slug').value = slug;
        document.getElementById('notes-form-title').value = note ? note.title : '';
        document.getElementById('notes-form-content').value = note ? note.content : '';
    } else {
        document.getElementById('notes-form-slug').value = '';
        document.getElementById('notes-form-title').value = '';
        document.getElementById('notes-form-content').value = '';
    }
    document.getElementById('notes-form-title').focus();
}

function closeNoteEditor() {
    document.getElementById('notes-form').hidden = true;
    document.getElementById('notes-list').hidden = false;
    document.getElementById('notes-add-btn').hidden = false;
    refreshNotes();
}

async function refreshNotes() {
    try {
        const notes = await listNotes();
        renderNotesList(notes);
    } catch (e) {
        alert('Failed to load notes: ' + e.message);
    }
}

// ============ Boot ============

document.addEventListener('DOMContentLoaded', () => {
    // Telegram SDK init (no-op if running in a regular browser).
    const tg = window.Telegram?.WebApp;
    if (tg) {
        tg.ready();
        tg.expand();
    }

    // Tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => showTab(btn.dataset.tab));
    });

    // Inventory wiring
    document.getElementById('inv-add-btn').addEventListener('click', () => showInventoryForm(null));
    document.getElementById('inv-form-cancel').addEventListener('click', hideInventoryForm);
    document.getElementById('inv-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const id = document.getElementById('inv-form-id').value || null;
        const name = document.getElementById('inv-form-name').value.trim();
        const qty = parseInt(document.getElementById('inv-form-qty').value, 10);
        const notes = document.getElementById('inv-form-notes').value;
        const errEl = document.getElementById('inv-form-error');

        if (!name) {
            errEl.textContent = 'Name is required.';
            errEl.hidden = false;
            return;
        }
        if (isNaN(qty) || qty < 0) {
            errEl.textContent = 'Quantity must be a non-negative integer.';
            errEl.hidden = false;
            return;
        }

        try {
            await saveItem(id, { name, qty, notes });
            hideInventoryForm();
            await refreshInventory();
        } catch (e) {
            errEl.textContent = 'Save failed: ' + e.message;
            errEl.hidden = false;
        }
    });

    // Notes wiring
    document.getElementById('notes-add-btn').addEventListener('click', () => openNoteEditor(null));
    document.getElementById('notes-form-cancel').addEventListener('click', closeNoteEditor);
    document.getElementById('notes-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const oldSlug = document.getElementById('notes-form-slug').value || null;
        const title = document.getElementById('notes-form-title').value.trim();
        const content = document.getElementById('notes-form-content').value;
        const errEl = document.getElementById('notes-form-error');

        if (!title) {
            errEl.textContent = 'Title is required.';
            errEl.hidden = false;
            return;
        }

        try {
            await saveNote(oldSlug, title, content);
            closeNoteEditor();
        } catch (e) {
            errEl.textContent = e.message;
            errEl.hidden = false;
        }
    });
    document.getElementById('notes-form-delete').addEventListener('click', async () => {
        const slug = document.getElementById('notes-form-slug').value;
        if (!slug) return;
        if (!confirm('Delete this note?')) return;
        try {
            await deleteNote(slug);
            closeNoteEditor();
        } catch (e) {
            alert('Delete failed: ' + e.message);
        }
    });

    // Initial load
    refreshInventory();
    refreshNotes();
});
