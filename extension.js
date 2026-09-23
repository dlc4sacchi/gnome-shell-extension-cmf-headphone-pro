import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';

const OPTIONS = {
    noise: ['ANC', 'Off', 'Transparency'],
    level: ['High', 'Mid', 'Low', 'Adaptive'],
    lag: ['Off', 'On'],
    spatial: ['Off', 'Concert', 'Cinema'],
    eq: ['Pop', 'Rock', 'Electronic', 'Enhance Vocals', 'Classical', 'Custom'],
    profile: ['Off', 'On'],
    find: [],
};
const TITLES = {
    noise: 'Noise control', level: 'ANC level', lag: 'Low lag',
    spatial: 'Spatial audio', eq: 'Equaliser',
    profile: 'Personal sound profile', find: 'Find my headphones',
};
const SETTING = {
    spatial: {Off: 0, Concert: 2, Cinema: 3},
    eq: {Pop: 3, Rock: 1, Electronic: 2, 'Enhance Vocals': 4, Classical: 5, Custom: 6},
};
const LEVELS = {High: 1, Mid: 2, Low: 3, Adaptive: 4};
const CONFIG_PATH = GLib.build_filenamev([
    GLib.get_user_config_dir(), 'cmf-headphone-pro', 'config.json',
]);

function readConfig() {
    let saved = {};
    try {
        const [, contents] = Gio.File.new_for_path(CONFIG_PATH).load_contents(null);
        saved = JSON.parse(new TextDecoder().decode(contents));
    } catch {
        // Missing or incomplete configuration uses safe defaults.
    }
    const items = {};
    for (const key of Object.keys(OPTIONS)) {
        const source = saved.items?.[key] ?? {};
        const selected = OPTIONS[key].filter(option => source.options?.includes(option));
        const minimum = key === 'profile' ? 1 : 2;
        items[key] = {
            enabled: typeof source.enabled === 'boolean'
                ? source.enabled : ['noise', 'level', 'lag', 'spatial'].includes(key),
            options: selected.length >= minimum ? selected : [...OPTIONS[key]],
        };
    }
    const order = Array.isArray(saved.order)
        ? [...new Set(saved.order.filter(key => key in OPTIONS))] : [];
    order.push(...Object.keys(OPTIONS).filter(key => !order.includes(key)));
    const active = order.filter(key => items[key].enabled);
    return {
        enabled: saved.enabled !== false,
        click: active.includes(saved.click) ? saved.click : (active[0] ?? ''),
        order, items,
    };
}

function runController(extension, args, callback) {
    const script = GLib.build_filenamev([extension.path, 'controller.py']);
    let process;
    try {
        process = Gio.Subprocess.new(['/usr/bin/python3', script, ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
    } catch (error) {
        callback(null, error.message);
        return;
    }
    process.communicate_utf8_async(null, null, (source, result) => {
        try {
            const [, output, errors] = source.communicate_utf8_finish(result);
            const report = JSON.parse(output);
            callback(source.get_successful() ? report : null,
                source.get_successful() ? null : report.error ?? errors.trim());
        } catch (error) {
            callback(null, error.message);
        }
    });
}

const CycleRow = GObject.registerClass(
class CycleRow extends PopupMenu.PopupBaseMenuItem {
    _init(title, onClick) {
        super._init({style_class: 'cmf-cycle-row'});
        this._onClick = onClick;
        this._title = new St.Label({
            text: title, x_expand: true, y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._title);
        this._value = new St.Label({
            text: '', style_class: 'cmf-cycle-value',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._value);
        this.label_actor = this._title;
    }

    activate() {
        this._onClick();
    }

    setValue(value) {
        this._value.text = value;
    }
});

const CmfTile = GObject.registerClass(
class CmfTile extends QuickSettings.QuickMenuToggle {
    _init(extension) {
        super._init({
            title: 'Noise control', subtitle: 'Connecting…',
            icon_name: 'audio-headphones-symbolic', toggleMode: true,
            'menu-button-accessible-name': 'CMF Headphone Pro controls',
        });
        this._extension = extension;
        this._config = readConfig();
        this._state = {noise: 'ANC', level: 'Adaptive', lag: 'Off', spatial: 'Off',
            eq: 'Rock', profile: 'Off'};
        this._battery = null;
        this._connected = false;
        this._busy = false;
        this._disposed = false;
        this._ringTimer = 0;
        this._rows = {};
        this.connect('clicked', () => {
            this._cycle(this._config.click);
            this.checked = this._connected;
        });
        this.menu.setHeader('audio-headphones-symbolic', 'CMF Headphone Pro', '');
        this._section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._section);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const controls = this.menu.addAction('Open controls…',
            () => this._extension.openControls());
        controls.visible = Main.sessionMode.allowSettings;
        this.menu._settingsActions[this._extension.uuid] = controls;
        this.reloadConfig();
        this._setConnected(false);
    }

    reloadConfig() {
        this._config = readConfig();
        this.visible = this._config.enabled;
        this._section.removeAll();
        this._rows = {};
        for (const key of this._config.order) {
            if (!this._config.items[key].enabled)
                continue;
            const row = new CycleRow(TITLES[key], () => this._cycle(key));
            this._section.addMenuItem(row);
            this._rows[key] = row;
        }
        this._render();
    }

    refresh() {
        if (this._busy)
            return;
        this._busy = true;
        runController(this._extension, ['quick'], (report, error) => {
            if (this._disposed)
                return;
            this._busy = false;
            if (error) {
                this._setConnected(false);
                return;
            }
            this._applyReport(report);
        });
    }

    _applyReport(report) {
        const mode = report.anc?.mode;
        if ([1, 2, 3, 4].includes(mode)) {
            this._state.noise = 'ANC';
            this._state.level = Object.keys(LEVELS).find(key => LEVELS[key] === mode);
        } else if (mode === 5) {
            this._state.noise = 'Off';
        } else if (mode === 7) {
            this._state.noise = 'Transparency';
        }
        if ([1, 2, 3, 4].includes(report.anc?.level))
            this._state.level = Object.keys(LEVELS).find(key => LEVELS[key] === report.anc.level);
        if (typeof report.low_lag === 'boolean')
            this._state.lag = report.low_lag ? 'On' : 'Off';
        if (typeof report.personal_sound === 'boolean')
            this._state.profile = report.personal_sound ? 'On' : 'Off';
        if (report.spatial !== undefined)
            this._state.spatial = Object.keys(SETTING.spatial)
                .find(key => SETTING.spatial[key] === report.spatial) ?? this._state.spatial;
        if (report.eq !== undefined)
            this._state.eq = Object.keys(SETTING.eq)
                .find(key => SETTING.eq[key] === report.eq) ?? this._state.eq;
        if (Number.isInteger(report.battery))
            this._battery = report.battery;
        this._setConnected(true);
    }

    _setConnected(connected) {
        this._connected = connected;
        this.reactive = connected;
        this.menuEnabled = connected;
        this.checked = connected;
        for (const child of this._box.get_children()) {
            child.reactive = connected;
            child.can_focus = connected;
        }
        this.opacity = connected ? 255 : 128;
        if (!connected)
            this.menu.close();
        this._render();
    }

    _render() {
        const action = this._config.click;
        this.title = TITLES[action] ?? 'CMF Headphone Pro';
        this.subtitle = this._connected
            ? `${this._battery ?? '—'}% · ${action === 'find' ? 'Play sound' : this._state[action] ?? ''}`
            : 'Disconnected';
        for (const [key, row] of Object.entries(this._rows))
            row.setValue(key === 'find' ? 'Play' : this._state[key]);
        this.menu.setHeader('audio-headphones-symbolic', 'CMF Headphone Pro',
            this._connected ? `${this._battery ?? '—'}% battery` : 'Disconnected');
    }

    _cycle(key) {
        if (!this._connected || this._busy || !this._config.items[key]?.enabled)
            return;
        if (key === 'find') {
            const ringing = this._ringTimer !== 0;
            this._send('ring', !ringing, () => {
                if (this._ringTimer)
                    GLib.Source.remove(this._ringTimer);
                this._ringTimer = ringing ? 0 : GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 10, () => {
                    this._ringTimer = 0;
                    this._send('ring', false);
                    return GLib.SOURCE_REMOVE;
                });
            });
            return;
        }
        const options = key === 'lag' ? OPTIONS.lag : this._config.items[key].options;
        const next = options[(options.indexOf(this._state[key]) + 1) % options.length];
        if (key === 'noise')
            this._send('anc', next === 'ANC' ? LEVELS[this._state.level] : (next === 'Off' ? 5 : 7));
        else if (key === 'level')
            this._send('anc', LEVELS[next]);
        else if (key === 'lag' || key === 'profile')
            this._send(key === 'lag' ? 'low_lag' : 'personal_sound', next === 'On');
        else
            this._send(key, SETTING[key][next]);
    }

    _send(name, value, onSuccess = null) {
        if (this._busy || !this._connected)
            return;
        this._busy = true;
        runController(this._extension, ['set', name, JSON.stringify(value)], (report, error) => {
            if (this._disposed)
                return;
            this._busy = false;
            if (error) {
                Main.notify('CMF Headphone Pro', `Could not change setting: ${error}`);
                this.refresh();
                return;
            }
            this._applyReport(report);
            onSuccess?.();
        });
    }

    destroy() {
        this._disposed = true;
        if (this._ringTimer)
            GLib.Source.remove(this._ringTimer);
        super.destroy();
    }
});

const CmfIndicator = GObject.registerClass(
class CmfIndicator extends QuickSettings.SystemIndicator {
    _init(extension) {
        super._init();
        this._tile = new CmfTile(extension);
        this.quickSettingsItems.push(this._tile);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class CmfHeadphoneExtension extends Extension {
    enable() {
        this._indicator = new CmfIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        const configDir = Gio.File.new_for_path(GLib.path_get_dirname(CONFIG_PATH));
        try {
            configDir.make_directory_with_parents(null);
        } catch (error) {
            if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
                console.error(error);
        }
        this._configMonitor = configDir.monitor_directory(Gio.FileMonitorFlags.NONE, null);
        this._configMonitor.connect('changed', (_monitor, file) => {
            if (file.get_basename() === 'config.json')
                this._indicator?._tile.reloadConfig();
        });
        this._indicator._tile.refresh();
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 15, () => {
            this._indicator?._tile.refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._pollId)
            GLib.Source.remove(this._pollId);
        this._pollId = 0;
        this._configMonitor?.cancel();
        this._configMonitor = null;
        this._indicator?.destroy();
        this._indicator = null;
    }

    openControls() {
        try {
            Gio.Subprocess.new(['/usr/bin/python3', GLib.build_filenamev([this.path, 'app.py'])],
                Gio.SubprocessFlags.NONE);
        } catch (error) {
            Main.notify('CMF Headphone Pro', `Could not open controls: ${error.message}`);
        }
    }
}
