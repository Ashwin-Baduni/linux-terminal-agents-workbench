// Drives the clipboard list with a virtual keyboard and pointer, checks each state and takes
// screenshots. Runs only in a throwaway headless GNOME Shell.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
const OUT = GLib.getenv('CLIPBOARD_LIST_OUT');
const wait = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => { resolve(); return GLib.SOURCE_REMOVE; }));
let failures = 0;
const log = line => {
    const stream = Gio.File.new_for_path(`${OUT}/log.txt`).append_to(Gio.FileCreateFlags.NONE, null);
    stream.write_all(new TextEncoder().encode(`${line}\n`), null);
    stream.close(null);
};
const check = (ok, what, detail = '') => {
    if (!ok) failures++;
    log(`${ok ? 'PASS' : 'FAIL'} ${what}${detail ? `  (${detail})` : ''}`);
};
async function shot(name) {
    const stream = Gio.File.new_for_path(`${OUT}/${name}.png`).replace(null, false, Gio.FileCreateFlags.NONE, null);
    await new Shell.Screenshot().screenshot(false, stream);
    stream.close(null);
}
const now = () => GLib.get_monotonic_time();

export default class Test extends Extension {
    enable() { this._run().catch(e => log(`ERROR ${e}\n${e.stack}`)); }
    disable() {}

    async key(keyval, mods = []) {
        for (const m of mods) this.kb.notify_keyval(now(), m, Clutter.KeyState.PRESSED);
        this.kb.notify_keyval(now(), keyval, Clutter.KeyState.PRESSED);
        this.kb.notify_keyval(now(), keyval, Clutter.KeyState.RELEASED);
        for (const m of mods.reverse()) this.kb.notify_keyval(now(), m, Clutter.KeyState.RELEASED);
        await wait(150);
    }
    async type(text) { for (const ch of text) await this.key(Clutter.unicode_to_keysym(ch.codePointAt(0))); }
    async moveTo(actor, dy = 0) {
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        this.ptr.notify_absolute_motion(now(), x + w / 2, y + h / 2 + dy);
        await wait(80);
        this.ptr.notify_absolute_motion(now(), x + w / 2 + 3, y + h / 2 + dy);
        await wait(200);
    }
    async click(actor) {
        await this.moveTo(actor);
        this.ptr.notify_button(now(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);
        this.ptr.notify_button(now(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED);
        await wait(700);
    }
    clipboard() {
        return new Promise(resolve => St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (_c, t) => resolve(t)));
    }
    visibleIn(scroll, actor) {
        const [sy] = [scroll.get_transformed_position()[1]];
        const sh = scroll.get_transformed_size()[1];
        const ay = actor.get_transformed_position()[1];
        const ah = actor.get_transformed_size()[1];
        return ay >= sy - 1 && ay + ah <= sy + sh + 1;
    }

    async _run() {
        await wait(2500);
        const seat = Clutter.get_default_backend().get_default_seat();
        this.kb = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this.ptr = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const ext = global.clipboardList;
        check(!!ext, 'extension loaded');
        const clip = St.Clipboard.get_default();
        const copy = text => clip.set_text(St.ClipboardType.CLIPBOARD, text);

        // Super+V opens and closes it, through the real key binding.
        await this.key(Clutter.KEY_v, [Clutter.KEY_Super_L]); await wait(700);
        check(!!ext._dialog, 'Super+V opens the list');
        await shot('1-empty');
        check(ext._dialog?.list.get_first_child()?.text === 'Nothing copied yet.', 'empty list says so');
        await this.key(Clutter.KEY_v, [Clutter.KEY_Super_L]); await wait(700);
        check(!ext._dialog, 'Super+V closes it again');

        const conversation = ['Conversation with Claude in ~/Desktop/example-site', '', 'You:',
            'Check the screenshots. The skills section looks very ugly.', '', 'Claude:', '  • ran: git status --short',
            '  • read: src/sections/About.tsx (×2)', '  • edited: src/index.css', ''];
        while (conversation.length < 4297) conversation.push(`  • ran: step ${conversation.length}`);
        const copies = ['Okay, so the first thing is the list of tasks\nthat need doing today\nin this order\nfourth\nfifth\nsixth',
            'git log --oneline -5 --format=%h %s', '~/Desktop/example-site/.github/workflows',
            'https://github.com/example/project', 'x', 'npm run verify', conversation.join('\n')];
        // One drag first, so it ends up the oldest copy.
        for (const text of ['Partial', 'Partial selec', 'Partial selection while dragging']) { copy(text); await wait(120); }
        await wait(1200);
        for (const text of copies) { copy(text); await wait(900); }
        check(ext.history.length === 7, 'single characters skipped, a drag is one copy', `${ext.history.length} copies`);
        // Drop the drag test entry and age the rest like the preview: 0, 2, 9, 25, 70, 190 minutes.
        const entries = ext.history.filter(e => !e.text.startsWith('Partial'));
        const real = GLib.get_real_time();
        [0, 2, 9, 25, 70, 190].forEach((minutes, i) => (entries[i].time = real - minutes * 60e6 - 5e6));
        const list = ext.history;
        list.forEach(e => { if (e.text.startsWith('Partial')) e.time = real - 400 * 60e6; });

        ext.toggle(); await wait(900);
        const d = ext._dialog;
        await shot('2-open');
        const rows = d.list.get_children();
        const details = row => row.details.get_children().map(l => l.text).join('');
        check(details(rows[0]) === 'text · 4,297 lines · just now', 'conversation details', details(rows[0]));
        check(details(rows[1]) === 'text · 2 min ago', 'npm details', details(rows[1]));
        check(details(rows[2]) === 'link · 9 min ago', 'link details', details(rows[2]));
        check(details(rows[3]) === 'path · 25 min ago', 'path details', details(rows[3]));
        check(details(rows[4]) === 'text · 1 h ago', 'older details', details(rows[4]));
        const ageClass = row => row.details.get_children().at(-1).get_style_class_name();
        check(ageClass(rows[2]).includes('fresh') && ageClass(rows[3]).includes('recent') && ageClass(rows[4]).includes('older'),
            'time colours: fresh, recent, older', [2, 3, 4].map(i => ageClass(rows[i])).join(' / '));
        check(d.count.text === '7 copies', 'count', d.count.text);
        check(rows[0].has_style_pseudo_class('selected'), 'newest copy selected on open');
        check(d.preview.text.endsWith('… 3,897 more lines. Copying takes all of it.'), 'long preview is cut with a note',
            d.preview.text.slice(-60));
        check(global.stage.get_key_focus() === d.search.clutter_text, 'typing goes to the search box');

        await this.key(Clutter.KEY_Down);
        check(d.selectedText === 'npm run verify' && rows[1].has_style_pseudo_class('selected') &&
              !rows[0].has_style_pseudo_class('selected'), 'Down selects the next copy', d.selectedText);
        check(d.preview.text === 'npm run verify', 'preview follows the selection');
        await shot('3-down');
        const bar = rows[1].child.get_first_child();
        const [bx, by] = bar.get_transformed_position();
        const [bw, bh] = bar.get_transformed_size();
        check(bw >= 2 && bh > 20 && bar.get_theme_node().get_background_color().to_string().startsWith('#ff91c8'),
            'the selected row has a rose bar', `${bw}x${bh} ${bar.get_theme_node().get_background_color().to_string()}`);
        check(!rows[0].child.get_first_child().get_theme_node().get_background_color().alpha, 'other rows have no bar');
        // Rows keep their natural height however few copies there are (no stretching into spare space).
        const heights = rows.slice(0, 6).map(row => Math.round(row.get_transformed_size()[1]));
        const natural = rows[1].get_preferred_height(-1)[1];
        check(heights.every(h => Math.abs(h - natural) <= 1), 'rows are not stretched', `${heights.join(',')} vs ${Math.round(natural)}`);
        await this.key(Clutter.KEY_Up); await this.key(Clutter.KEY_Up);
        check(d.selectedText?.startsWith('Conversation'), 'Up stops at the first copy');
        await this.key(Clutter.KEY_Page_Down);
        check(d._selected === rows.length - 1, 'Page Down stops at the last copy', `${d._selected}`);

        await this.type('git');
        const matches = d.list.get_children().length;
        check(d.count.text === `${matches} of 7 copies` && matches === 4, 'search filters', `${d.count.text}`);
        check(d._selected === 0, 'search selects its first match');
        await shot('4-search');
        await this.type('zzz');
        check(d.list.get_first_child().text === 'No copies match.' && d.count.text === '0 of 7 copies', 'no matches says so');
        await this.key(Clutter.KEY_Return);
        check(!!ext._dialog, 'Enter with no matches does nothing');
        await shot('5-nomatch');
        for (let i = 0; i < 6; i++) await this.key(Clutter.KEY_BackSpace);
        check(d.list.get_children().length === 7, 'clearing the search shows everything again');
        await this.key(Clutter.KEY_Down); await this.key(Clutter.KEY_Down);
        const want = d.selectedText;
        await this.key(Clutter.KEY_Return); await wait(900);
        check(!ext._dialog, 'Enter closes the list');
        check(await this.clipboard() === want, 'Enter copies the selected copy', want);
        await wait(900);
        check(ext.history[0].text === want, 'the copy picked moves to the top');

        // The mouse: hover selects, click copies.
        ext.toggle(); await wait(900);
        let r = ext._dialog.list.get_children();
        await this.moveTo(r[3]);
        check(ext._dialog.selectedText === r[3].entry.text, 'hovering selects', ext._dialog.selectedText);
        await shot('6-hover');
        const clicked = r[4].entry.text;
        await this.click(r[4]); await wait(400);
        check(!ext._dialog && await this.clipboard() === clicked, 'clicking copies and closes', clicked);
        await wait(900);

        // The close button and Esc.
        ext.toggle(); await wait(900);
        await this.click(ext._dialog.closeButton);
        check(!ext._dialog, 'the ✕ closes the list');
        ext.toggle(); await wait(900);
        await this.key(Clutter.KEY_Escape); await wait(700);
        check(!ext._dialog, 'Esc closes the list');

        // A long day: more than the 200 copies the list draws.
        for (let i = 0; i < 205; i++) { copy(`copy number ${i} with some words`); await wait(560); }
        check(ext.history.length === 212, 'no fixed limit: every copy kept', `${ext.history.length}`);
        ext.toggle(); await wait(1200);
        r = ext._dialog.list.get_children();
        check(ext._dialog._rows.length === 200 && r.at(-1).text === '12 older copies: search to find them',
            'the list draws the newest 200 and says how many are older', `${ext._dialog._rows.length} rows, last: ${r.at(-1).text}`);
        check(ext._dialog.count.text === '212 copies', 'count', ext._dialog.count.text);
        await this.moveTo(r[2]);
        check(ext._dialog._selected === 2, 'the pointer selects the row under it', `${ext._dialog._selected}`);
        // The list scrolls under the pointer, which then rests over other rows; they must not take over.
        for (let i = 0; i < 30; i++) await this.key(Clutter.KEY_Down);
        await wait(500);
        const sel = ext._dialog._rows[ext._dialog._selected];
        check(ext._dialog._selected === 32, 'arrows win over a resting pointer', `${ext._dialog._selected}`);
        check(this.visibleIn(ext._dialog._scroll, sel), 'the selected copy stays in view');
        await shot('7-scrolled');
        await this.type('Okay, so the first');
        check(ext._dialog._rows.length === 1 && ext._dialog.selectedText?.startsWith('Okay, so the first'),
            'search finds a copy older than the newest 200', ext._dialog.count.text);
        await shot('8-old-found');
        await this.key(Clutter.KEY_Escape); await wait(700);

        // The size cap (lowered for this test to 600,000 characters): the oldest copies go first.
        const big = n => `blob ${n}\n` + 'x'.repeat(100000);
        for (let i = 0; i < 7; i++) { copy(big(i)); await wait(900); }
        const total = ext.history.reduce((sum, e) => sum + e.text.length, 0);
        check(total <= 600000 && ext.history[0].text.startsWith('blob 6') && !ext.history.some(e => e.text.startsWith('Conversation')),
            'past the size cap the oldest copies are dropped', `${ext.history.length} copies, ${total} characters`);

        ext.disable(); ext.enable(); await wait(900);  // what a screen lock does
        check(ext.history[0].text.startsWith('blob 6') && ext.history.length > 1, 'the list survives a screen lock');
        log(`DONE failures=${failures}`);
    }
}
