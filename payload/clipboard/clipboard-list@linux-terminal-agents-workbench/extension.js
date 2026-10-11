// Super+V shows what you copied recently, newest first. Click one, or press Enter, to copy it
// again; Esc or Super+V closes the list. Text only.
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

const LIMIT = 50;
// Selecting in a terminal copies as you drag; waiting for the selection to settle makes one
// drag one entry.
const SETTLE_MS = 500;
const history = [];

function remember(text) {
    // Empty text and single characters are almost always a stray click, not something to keep.
    if (!text || text.trim().length < 2)
        return;
    const index = history.indexOf(text);
    if (index !== -1)
        history.splice(index, 1);
    history.unshift(text);
    history.length = Math.min(history.length, LIMIT);
}

function describe(text) {
    const lines = text.split('\n');
    let label = (lines.find(line => line.trim()) ?? '').trim().replace(/\s+/g, ' ');
    if (label.length > 160)
        label = `${label.slice(0, 159)}…`;
    const more = lines.filter(line => line.trim()).length - 1;
    return [label, more > 0 ? `+ ${more} more line${more === 1 ? '' : 's'}` : ''];
}

const ClipboardDialog = GObject.registerClass(
class ClipboardDialog extends ModalDialog.ModalDialog {
    _init(onPick) {
        super._init({styleClass: 'clipboard-list-dialog', actionMode: Shell.ActionMode.POPUP});
        this.contentLayout.add_child(new St.Label({text: 'Clipboard', style_class: 'clipboard-list-title'}));
        this.list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL});
        this._scroll = new St.ScrollView({
            style_class: 'clipboard-list-scroll',
            child: this.list,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        this.contentLayout.add_child(this._scroll);
        if (!history.length) {
            this.list.add_child(new St.Label({text: 'Nothing copied yet.', style_class: 'clipboard-list-empty'}));
            return;
        }
        for (const text of history) {
            const [label, more] = describe(text);
            const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true});
            const main = new St.Label({text: label, x_expand: true});
            main.clutter_text.ellipsize = Pango.EllipsizeMode.END;
            box.add_child(main);
            if (more)
                box.add_child(new St.Label({text: more, style_class: 'clipboard-list-meta'}));
            const item = new St.Button({
                child: box,
                style_class: 'clipboard-list-item',
                can_focus: true,
                x_expand: true,
            });
            item.connect('clicked', () => {
                onPick(text);
                this.close();
            });
            item.connect('key-focus-in', () => ensureActorVisibleInScrollView(this._scroll, item));
            this.list.add_child(item);
        }
        this.setInitialKeyFocus(this.list.get_first_child());
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

    get history() {
        return [...history];
    }
}
