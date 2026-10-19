// Super+V shows what you copied recently, newest first: the list on the left, the full text of
// the selected copy on the right. Type to search; click a copy, or press Enter, to copy it again.
// Esc, the ✕ or Super+V closes the list. Text only.
//
// The list is kept in memory, never on disk, so it is forgotten when you log out. It lives at
// module level because GNOME disables extensions while the screen is locked; that way the list
// survives a lock.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import {ensureActorVisibleInScrollView} from 'resource:///org/gnome/shell/misc/animationUtils.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// No fixed number of copies: everything since login is kept, until the copies together reach
// about 32 MB (16 million characters); then the oldest go first. The tests lower this.
const MAX_CHARS = Number(GLib.getenv('CLIPBOARD_LIST_MAX_CHARS')) || 16_000_000;
// The list draws the newest 200 copies so it opens instantly; search covers all of them.
const SHOWN = 200;
// Selecting in a terminal copies as you drag; waiting for the selection to settle makes one
// drag one entry.
const SETTLE_MS = 500;
// The preview shows the start of a very long copy; picking it always copies all of it.
const PREVIEW_LINES = 400;
const PREVIEW_CHARS = 40000;
// Page Up and Page Down move this many copies.
const PAGE = 8;
// Newest first: {text, time}, time in microseconds of wall-clock time, so a suspend counts.
const history = [];

function remember(text) {
    // Empty text and single characters are almost always a stray click, not something to keep.
    if (!text || text.trim().length < 2)
        return;
    const index = history.findIndex(entry => entry.text === text);
    if (index !== -1)
        history.splice(index, 1);
    history.unshift({text, time: GLib.get_real_time()});
    let total = history.reduce((sum, entry) => sum + entry.text.length, 0);
    while (history.length > 1 && total > MAX_CHARS)
        total -= history.pop().text.length;
}

const thousands = number => String(number).replace(/\B(?=(\d{3})+(?!\d))/g, ',');

function lineCount(text) {
    return text.replace(/\n+$/, '').split('\n').length;
}

function firstLine(text) {
    const line = (text.split('\n').find(part => part.trim()) ?? '').trim().replace(/\s+/g, ' ');
    return line.length > 200 ? `${line.slice(0, 199)}…` : line;
}

// What a copy is, in one word: a web address, a file path, or anything else.
function kind(text) {
    const trimmed = text.trim();
    if (/^(https?|ftp):\/\/\S+$/i.test(trimmed) || /^www\.\S+\.\S+$/i.test(trimmed))
        return 'link';
    if (/^(~|\.{1,2})?\/\S*$/.test(trimmed))
        return 'path';
    return 'text';
}

// How long ago, and its colour: green for the last 10 minutes, blue for the last hour, coral
// for anything older.
function age(time, now) {
    const minutes = Math.max(0, Math.floor((now - time) / 60e6));
    if (minutes < 1)
        return ['just now', 'fresh'];
    if (minutes < 60)
        return [`${minutes} min ago`, minutes < 10 ? 'fresh' : 'recent'];
    return [`${Math.floor(minutes / 60)} h ago`, 'older'];
}

const ClipboardDialog = GObject.registerClass(
class ClipboardDialog extends ModalDialog.ModalDialog {
    _init(onPick) {
        super._init({styleClass: 'clipboard-list-dialog', actionMode: Shell.ActionMode.POPUP});
        this._onPick = onPick;
        this._now = GLib.get_real_time();
        this._rows = [];
        this._selected = -1;
        this.buttonLayout.visible = false;

        // Search on the left, close on the right.
        const header = new St.BoxLayout({style_class: 'clipboard-list-header'});
        this.search = new St.Entry({
            style_class: 'clipboard-list-search',
            hint_text: 'Type to search…',
            can_focus: true,
            x_expand: true,
        });
        this.search.set_primary_icon(new St.Icon({
            icon_name: 'edit-find-symbolic',
            style_class: 'clipboard-list-search-icon',
        }));
        header.add_child(this.search);
        this.closeButton = new St.Button({
            style_class: 'clipboard-list-close',
            label: '✕',
            accessible_name: 'Close',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.closeButton.connect('clicked', () => this.close());
        header.add_child(this.closeButton);
        this.contentLayout.add_child(header);

        // The copies on the left, the selected copy's full text on the right.
        const body = new St.BoxLayout({style_class: 'clipboard-list-body'});
        this.list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true});
        this._scroll = new St.ScrollView({
            style_class: 'clipboard-list-scroll',
            child: this.list,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        body.add_child(this._scroll);
        this.preview = new St.Label({style_class: 'clipboard-list-preview-text', x_expand: true});
        this.preview.clutter_text.line_wrap = true;
        this.preview.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        this.preview.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        const page = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true});
        page.add_child(this.preview);
        this._previewScroll = new St.ScrollView({
            style_class: 'clipboard-list-preview',
            child: page,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            x_expand: true,
        });
        body.add_child(this._previewScroll);
        this.contentLayout.add_child(body);

        // How to use it on the left; how many copies, and the list's name, on the right.
        const footer = new St.BoxLayout({style_class: 'clipboard-list-footer'});
        footer.add_child(new St.Label({
            text: 'Click or Enter copies · ↑ ↓ move · Esc closes',
            style_class: 'clipboard-list-hint',
            x_expand: true,
        }));
        this.count = new St.Label({style_class: 'clipboard-list-hint clipboard-list-count'});
        footer.add_child(this.count);
        footer.add_child(new St.Label({text: 'Clipboard', style_class: 'clipboard-list-name'}));
        this.contentLayout.add_child(footer);

        // Typing always goes to the search box, so the arrows, Enter and Esc are handled there.
        this.search.clutter_text.connect('text-changed', () => this._fill());
        this.search.clutter_text.connect('key-press-event', (_actor, event) => this._key(event));
        this._fill();
        this.setInitialKeyFocus(this.search);
    }

    _fill() {
        const query = this.search.get_text().trim().toLowerCase();
        const shown = query ? history.filter(entry => entry.text.toLowerCase().includes(query)) : history;
        this.list.destroy_all_children();
        this._rows = shown.slice(0, SHOWN).map(entry => this._row(entry));
        this._rows.forEach(row => this.list.add_child(row));
        const hidden = shown.length - this._rows.length;
        if (hidden > 0) {
            this.list.add_child(new St.Label({
                text: query ? `${thousands(hidden)} more ${hidden === 1 ? 'match' : 'matches'}: type more to narrow it down`
                    : `${thousands(hidden)} older ${hidden === 1 ? 'copy' : 'copies'}: search to find ${hidden === 1 ? 'it' : 'them'}`,
                style_class: 'clipboard-list-empty',
            }));
        }
        if (!shown.length) {
            this.list.add_child(new St.Label({
                text: history.length ? 'No copies match.' : 'Nothing copied yet.',
                style_class: 'clipboard-list-empty',
            }));
        }
        const total = `${thousands(history.length)} ${history.length === 1 ? 'copy' : 'copies'}`;
        this.count.text = query ? `${thousands(shown.length)} of ${total}` : total;
        this._selected = -1;
        this._select(0);
    }

    _row(entry) {
        // A thin bar on the left that turns rose when the copy is selected.
        const line = new St.BoxLayout({x_expand: true});
        line.add_child(new St.Widget({style_class: 'clipboard-list-bar'}));
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style_class: 'clipboard-list-copy'});
        line.add_child(box);
        const label = new St.Label({text: firstLine(entry.text), style_class: 'clipboard-list-text', x_expand: true});
        label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        box.add_child(label);
        const details = new St.BoxLayout({style_class: 'clipboard-list-details'});
        const add = (text, style) => details.add_child(new St.Label({text, style_class: style}));
        add(kind(entry.text), 'clipboard-list-kind');
        const lines = lineCount(entry.text);
        if (lines > 1) {
            add(' · ', 'clipboard-list-dot');
            add(`${thousands(lines)} lines`, 'clipboard-list-size');
        }
        const [when, colour] = age(entry.time, this._now);
        add(' · ', 'clipboard-list-dot');
        add(when, `clipboard-list-age ${colour}`);
        box.add_child(details);
        const row = new St.Button({
            child: line,
            style_class: 'clipboard-list-item',
            can_focus: false,
            x_expand: true,
            x_align: Clutter.ActorAlign.FILL,
        });
        row.entry = entry;
        row.details = details;
        row.connect('clicked', () => this._pick(entry));
        // Only real pointer movement selects: the list scrolling under a still pointer must not
        // move the selection away from where the arrow keys put it.
        row.connect('motion-event', () => {
            this._select(this._rows.indexOf(row), false);
            return Clutter.EVENT_PROPAGATE;
        });
        return row;
    }

    _select(index, scroll = true) {
        if (!this._rows.length) {
            this._selected = -1;
            this._show(null);
            return;
        }
        index = Math.max(0, Math.min(index, this._rows.length - 1));
        if (index === this._selected)
            return;
        this._rows[this._selected]?.remove_style_pseudo_class('selected');
        this._selected = index;
        const row = this._rows[index];
        row.add_style_pseudo_class('selected');
        if (scroll)
            ensureActorVisibleInScrollView(this._scroll, row);
        this._show(row.entry);
    }

    _show(entry) {
        let text = entry?.text ?? '';
        const lines = text.split('\n');
        let more = 0;
        if (lines.length > PREVIEW_LINES) {
            more = lines.length - PREVIEW_LINES;
            text = lines.slice(0, PREVIEW_LINES).join('\n');
        }
        if (text.length > PREVIEW_CHARS)
            text = `${text.slice(0, PREVIEW_CHARS)}…`;
        if (more)
            text += `\n\n… ${thousands(more)} more ${more === 1 ? 'line' : 'lines'}. Copying takes all of it.`;
        this.preview.text = text;
        this._previewScroll.vadjustment.value = 0;
    }

    get selectedText() {
        return this._rows[this._selected]?.entry.text ?? null;
    }

    _pick(entry) {
        this._onPick(entry.text);
        this.close();
    }

    _key(event) {
        switch (event.get_key_symbol()) {
        case Clutter.KEY_Down:
            this._select(this._selected + 1);
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Up:
            this._select(this._selected - 1);
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Page_Down:
            this._select(this._selected + PAGE);
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Page_Up:
            this._select(this._selected - PAGE);
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Return:
        case Clutter.KEY_KP_Enter:
        case Clutter.KEY_ISO_Enter:
            if (this._rows[this._selected])
                this._pick(this._rows[this._selected].entry);
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Escape:
            this.close();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    vfunc_key_press_event(event) {
        if (event.get_key_symbol() === Clutter.KEY_Escape) {
            this.close();
            return Clutter.EVENT_STOP;
        }
        return super.vfunc_key_press_event(event);
    }
});

export default class ClipboardListExtension extends Extension {
    enable() {
        this._clipboard = St.Clipboard.get_default();
        this._selection = global.display.get_selection();
        this._ownerChangedId = this._selection.connect('owner-changed', (_selection, type) => {
            if (type === Meta.SelectionType.SELECTION_CLIPBOARD)
                this._settle();
        });
        this._settings = this.getSettings();
        Main.wm.addKeybinding('open', this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP,
            () => this.toggle());
        this._settle();
        if (GLib.getenv('CLIPBOARD_LIST_TEST'))
            global.clipboardList = this;
    }

    disable() {
        Main.wm.removeKeybinding('open');
        this._selection.disconnect(this._ownerChangedId);
        if (this._timeout)
            GLib.source_remove(this._timeout);
        this._timeout = 0;
        this._dialog?.close();
        this._dialog = null;
        this._settings = null;
        this._selection = null;
        this._clipboard = null;
        delete global.clipboardList;
    }

    _settle() {
        if (this._timeout)
            GLib.source_remove(this._timeout);
        this._timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SETTLE_MS, () => {
            this._timeout = 0;
            this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => remember(text));
            return GLib.SOURCE_REMOVE;
        });
    }

    toggle() {
        if (this._dialog) {
            this._dialog.close();
            return;
        }
        this._dialog = new ClipboardDialog(text => this._clipboard.set_text(St.ClipboardType.CLIPBOARD, text));
        this._dialog.connect('destroy', () => {
            this._dialog = null;
        });
        this._dialog.open();
    }

    // The copies themselves, newest first (for the tests).
    get history() {
        return [...history];
    }
}
