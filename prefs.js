import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class CmfHeadphonePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        // GNOME's settings button opens the same controls window as Quick Settings.
        const page = new Adw.PreferencesPage({title: 'Controls'});
        const group = new Adw.PreferencesGroup();
        const row = new Adw.ActionRow({title: 'Open controls', activatable: true});
        row.connect('activated', () => this._openControls(window));
        group.add(row);
        page.add(group);
        window.add(page);

        GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._openControls(window);
            return GLib.SOURCE_REMOVE;
        });
    }

    _openControls(window) {
        try {
            Gio.Subprocess.new([
                '/usr/bin/python3', GLib.build_filenamev([this.path, 'app.py']),
            ], Gio.SubprocessFlags.NONE);
            window.close();
        } catch (error) {
            console.error(`Could not open CMF Headphone Pro controls: ${error.message}`);
        }
    }
}
