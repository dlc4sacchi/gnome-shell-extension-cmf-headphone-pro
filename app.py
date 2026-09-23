#!/usr/bin/env python3
"""GTK4/libadwaita controls for CMF Headphone Pro."""

from pathlib import Path
import json
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import controller
from config import ITEMS as QUICK_ITEMS, load as load_quick_config, save as save_quick_config


HERE = Path(__file__).resolve().parent
PROJECT_URL = "https://github.com/dlc4sacchi/gnome-shell-extension-cmf-headphone-pro"
NOISE_CYCLES = {
    10: frozenset(("ANC", "Transparency", "Off")),
    20: frozenset(("ANC", "Off")),
    21: frozenset(("Transparency", "Off")),
    22: frozenset(("Transparency", "ANC")),
}


class ControlsWindow(Adw.ApplicationWindow):
    def __init__(self, app, disconnected=False):
        super().__init__(application=app, title="CMF Headphone Pro")
        self._setting_groups = []
        self._connected = True
        self._refreshing = False
        self._applying_state = False
        self._writing = False
        self._noise_change = False
        self._eq_timer = None
        self._physical_cycles = {}
        self._quick_config = load_quick_config()
        self._editing_quick_config = False
        self._pages = []
        self._search_results = []
        self.set_default_size(820, 580)
        self._stack = Adw.ViewStack()
        self._content_stack = Gtk.Stack()
        self._content_stack.add_named(self._stack, "pages")
        search_page = Adw.PreferencesPage()
        self._search_group = Adw.PreferencesGroup(title="Search results")
        search_page.add(self._search_group)
        self._empty_search = Adw.PreferencesGroup(title="No matching settings")
        self._empty_search.set_visible(False)
        search_page.add(self._empty_search)
        self._content_stack.add_named(search_page, "results")
        self._switcher = Adw.ViewSwitcher(stack=self._stack)
        self._switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self._switcher_bar = Adw.ViewSwitcherBar(stack=self._stack, reveal=False)
        self._header = Adw.HeaderBar()
        self._header.set_title_widget(self._switcher)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._header)
        self._search_bar = Gtk.SearchBar()
        self._search_entry = Gtk.SearchEntry(placeholder_text="Search settings")
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_key_capture_widget(self)
        self._search_bar.connect("notify::search-mode-enabled", self._search_mode_changed)
        self._search_entry.connect("search-changed", self._search_changed)
        self._search_entry.connect("activate", self._activate_first_result)
        toolbar.add_top_bar(self._search_bar)
        toolbar.set_content(self._content_stack)
        toolbar.add_bottom_bar(self._switcher_bar)
        self._overlay = Adw.ToastOverlay()
        self._overlay.set_child(toolbar)
        self.set_content(self._overlay)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 650sp"))
        narrow.add_setter(self._switcher, "visible", False)
        narrow.add_setter(self._switcher_bar, "reveal", True)
        self.add_breakpoint(narrow)
        self._install_css()
        self._build_noise_page()
        self._build_sound_page()
        self._build_controls_page()
        self._build_extension_page()
        self._install_refresh_button()
        self._install_about_button()
        self._connect_controls()
        self._set_connected(False)

    def _install_css(self):
        css = b"""
        .headphone-hero { padding: 0 16px 0; }
        .headphone-status { font-size: 16px; font-weight: 500; opacity: .78; }
        .headphone-mode { font-size: 19px; font-weight: 700; }
        viewswitcher.narrow button label { font-size: 11px; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _toast(self, message):
        self._overlay.add_toast(Adw.Toast.new(message))

    def _page(self, title, icon):
        page = Adw.PreferencesPage(title=title, icon_name=icon)
        self._stack.add_titled(page, title.lower(), title).set_icon_name(icon)
        self._pages.append((title, page))
        return page

    def _search_mode_changed(self, bar, _property):
        active = bar.get_search_mode()
        self._content_stack.set_visible_child_name("results" if active else "pages")
        if not active:
            self._search_entry.set_text("")

    def _search_changed(self, entry):
        for row in self._search_results:
            self._search_group.remove(row)
        self._search_results.clear()
        query = entry.get_text().strip().casefold()
        if not query:
            self._empty_search.set_visible(False)
            return

        def visit(widget, page_name, page, group_name=""):
            if isinstance(widget, Adw.PreferencesGroup):
                group_name = widget.get_title() or group_name
            if isinstance(widget, Adw.PreferencesRow):
                title = widget.get_title()
                detail = f"{page_name} · {group_name}" if group_name else page_name
                if title and query in f"{title} {detail}".casefold():
                    result = Adw.ActionRow(title=title, subtitle=detail)
                    result.set_activatable(True)
                    result.connect("activated", lambda _row, target=page, original=widget:
                                   self._open_search_result(target, original))
                    self._search_group.add(result)
                    self._search_results.append(result)
            child = widget.get_first_child()
            while child is not None:
                visit(child, page_name, page, group_name)
                child = child.get_next_sibling()

        for page_name, page in self._pages:
            visit(page, page_name, page)
        self._empty_search.set_visible(not self._search_results)

    def _activate_first_result(self, _entry):
        if self._search_results:
            self._search_results[0].activate()

    def _open_search_result(self, page, row):
        self._stack.set_visible_child(page)
        self._search_bar.set_search_mode(False)
        if row.get_sensitive() and row.get_visible():
            row.grab_focus()

    def _group(self, page, title, description=None, device_dependent=True):
        group = Adw.PreferencesGroup(title=title, description=description)
        page.add(group)
        if title and device_dependent:
            self._setting_groups.append(group)
        return group

    def _install_refresh_button(self):
        self._refresh_button = Gtk.Button()
        self._refresh_button.add_css_class("flat")
        self._refresh_button.set_tooltip_text("Refresh headset settings")
        self._refresh_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Refresh headset settings"]
        )
        self._refresh_stack = Gtk.Stack()
        self._refresh_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._refresh_stack.set_transition_duration(150)
        icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        self._refresh_spinner = Gtk.Spinner()
        self._refresh_stack.add_named(icon, "icon")
        self._refresh_stack.add_named(self._refresh_spinner, "spinner")
        self._refresh_stack.set_visible_child_name("icon")
        self._refresh_button.set_child(self._refresh_stack)
        self._refresh_button.connect("clicked", lambda _button: self.refresh_state())
        self._header.pack_start(self._refresh_button)

    def _install_about_button(self):
        button = Gtk.Button(icon_name="help-about-symbolic")
        button.add_css_class("flat")
        button.set_tooltip_text("About CMF Headphone Pro Controls")
        button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["About CMF Headphone Pro Controls"]
        )
        button.connect("clicked", self._show_about)
        self._header.pack_end(button)

    def _show_about(self, _button):
        dialog = Adw.AboutDialog(
            application_name="CMF Headphone Pro Controls",
            application_icon="audio-headphones-symbolic",
            version="1.0.0",
            developer_name="Saintin Roy Sacchi",
            website=PROJECT_URL,
            issue_url=f"{PROJECT_URL}/issues",
            license_type=Gtk.License.GPL_3_0,
            comments="Independent GNOME extension for CMF Headphone Pro.",
        )
        dialog.present(self)

    def refresh_state(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_button.set_sensitive(False)
        self._refresh_spinner.start()
        self._refresh_stack.set_visible_child_name("spinner")

        def worker():
            try:
                state = controller.main(["read"])
                error = None
            except Exception as exc:
                state = None
                error = exc
            GLib.idle_add(self._finish_refresh, state, error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh(self, state, error):
        self._refresh_spinner.stop()
        self._refresh_stack.set_visible_child_name("icon")
        self._refresh_button.set_sensitive(True)
        self._refreshing = False
        if error is not None:
            self._set_connected(False)
            self._toast(f"Could not refresh: {error}")
            return GLib.SOURCE_REMOVE
        self._apply_state(state)
        self._set_connected(True)
        if state.get("errors"):
            self._toast("Some headset settings could not be read")
        return GLib.SOURCE_REMOVE

    def _set_connected(self, connected):
        self._connected = connected
        for group in self._setting_groups:
            group.set_sensitive(connected)
        self._hero_image.set_opacity(1 if connected else 0.7)
        self._hero_status.set_text(
            f"Connected · {getattr(self, '_battery', 40)}%" if connected else "Disconnected"
        )
        if not connected:
            self._connection_row.set_subtitle("Disconnected")
        if connected:
            self._update_headline()
        else:
            self._hero_mode.set_text("CMF Headphone Pro")

    def _apply_state(self, state):
        self._applying_state = True
        try:
            battery = state.get("battery")
            if battery is not None:
                self._battery = battery
                self._connection_row.set_subtitle(f"Connected · {battery}%")
            anc = state.get("anc") or {}
            anc_mode = anc.get("mode")
            last_level = anc.get("level")
            if last_level in (1, 2, 3, 4):
                self._anc_level.set_selected({1: 0, 2: 1, 3: 2, 4: 3}[last_level])
            if anc_mode in (1, 2, 3, 4):
                self._noise_mode.set_selected(0)
                self._anc_level.set_selected({1: 0, 2: 1, 3: 2, 4: 3}[anc_mode])
            elif anc_mode == 7:
                self._noise_mode.set_selected(1)
            elif anc_mode == 5:
                self._noise_mode.set_selected(2)
            spatial = state.get("spatial")
            if spatial in (0, 2, 3):
                self._spatial.set_selected({0: 0, 2: 1, 3: 2}[spatial])
            eq = state.get("eq")
            if eq in (1, 2, 3, 4, 5, 6):
                self._eq_preset.set_selected({1: 1, 2: 2, 3: 0, 4: 3, 5: 4, 6: 5}[eq])
            custom = state.get("custom_eq") or {}
            for name, row in self._eq_rows.items():
                if name in custom:
                    value = custom[name]
                    row.set_snap_to_ticks(False)
                    row.set_digits(1 if value != round(value) else 0)
                    row.set_value(value)
            for name, row in (("personal_sound", self._personal_sound),
                              ("low_lag", self._low_lag),
                              ("dual_connection", self._dual_connection)):
                if name in state:
                    row.set_active(state[name])
            standby = state.get("standby")
            if standby in (30, 60, 120, 180, 240):
                self._standby.set_selected({30: 0, 60: 1, 120: 2, 180: 3, 240: 4}[standby])
            gestures = state.get("gestures") or {}
            actions = {1: 0, 10: 1, 20: 1, 21: 1, 22: 1, 11: 2, 27: 3, 29: 4}
            for key, row in (("button_single", self._button_single),
                             ("button_hold", self._button_hold)):
                if gestures.get(key) in actions:
                    if gestures[key] in NOISE_CYCLES:
                        self._set_physical_cycle(key, gestures[key])
                    row.set_selected(actions[gestures[key]])
            if gestures.get("slider") in (35, 36):
                self._slider_tuning.set_selected(0 if gestures["slider"] == 35 else 1)
            if gestures.get("roller_hold") in (1, 10, 20, 21, 22):
                if gestures["roller_hold"] in NOISE_CYCLES:
                    self._set_physical_cycle("roller_hold", gestures["roller_hold"])
                self._roller_hold.set_selected(0 if gestures["roller_hold"] == 1 else 1)
        finally:
            self._applying_state = False
        self._update_headline()

    def _send(self, name, value):
        if self._applying_state or self._writing or not self._connected:
            return
        self._writing = True
        self._refresh_button.set_sensitive(False)
        for group in self._setting_groups:
            group.set_sensitive(False)

        def worker():
            try:
                state = controller.main(["set", name, json.dumps(value)])
                error = None
            except Exception as exc:
                state, error = None, exc
            GLib.idle_add(self._finish_write, state, error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_write(self, state, error):
        self._writing = False
        self._refresh_button.set_sensitive(True)
        for group in self._setting_groups:
            group.set_sensitive(self._connected)
        if error:
            self._toast(f"Could not apply setting: {error}")
            self.refresh_state()
        else:
            self._apply_state(state)
            if state.get("errors"):
                self._toast("Some headset settings could not be read")
        return GLib.SOURCE_REMOVE

    def _connect_controls(self):
        self._noise_mode.connect("notify::selected", self._noise_mode_changed)
        self._anc_level.connect("notify::selected", self._anc_level_changed)
        self._spatial.connect("notify::selected", lambda row, _prop:
                              self._send("spatial", [0, 2, 3][row.get_selected()]))
        self._eq_preset.connect("notify::selected", lambda row, _prop:
                                self._send("eq", [3, 1, 2, 4, 5, 6][row.get_selected()]))
        for row, name in ((self._personal_sound, "personal_sound"),
                          (self._low_lag, "low_lag"),
                          (self._dual_connection, "dual_connection")):
            row.connect("notify::active", lambda widget, _prop, key=name:
                        self._send(key, widget.get_active()))
        self._standby.connect("notify::selected", lambda row, _prop:
                              self._send("standby", [30, 60, 120, 180, 240][row.get_selected()]))
        for row, name in ((self._button_single, "button_single"),
                          (self._button_hold, "button_hold")):
            row.connect("notify::selected", lambda widget, _prop, key=name:
                        self._send(key, self._button_code(key, widget.get_selected())))
        self._slider_tuning.connect("notify::selected", lambda row, _prop:
                                    self._send("slider", [35, 36][row.get_selected()]))
        self._roller_hold.connect("notify::selected", lambda row, _prop:
                                  self._send("roller_hold", 1 if row.get_selected() == 0
                                             else self._physical_cycle_code("roller_hold")))
        for row in self._eq_rows.values():
            row.connect("notify::value", self._custom_eq_changed)

    def _button_code(self, key, selected):
        return self._physical_cycle_code(key) if selected == 1 else [1, None, 11, 27, 29][selected]

    def _physical_cycle_code(self, key):
        rows = self._physical_cycles[key][1]
        selected = frozenset(name for name, row in rows.items() if row.get_active())
        return next(code for code, choices in NOISE_CYCLES.items() if selected == choices)

    def _set_physical_cycle(self, key, code):
        for name, row in self._physical_cycles[key][1].items():
            row.set_active(name in NOISE_CYCLES[code])

    def _physical_cycle_changed(self, key, row):
        if self._applying_state:
            return
        enabled = [choice for choice in self._physical_cycles[key][1].values() if choice.get_active()]
        if len(enabled) < 2:
            self._applying_state = True
            try:
                row.set_active(True)
            finally:
                self._applying_state = False
            self._toast("Keep at least two modes")
            return
        self._send(key, self._physical_cycle_code(key))

    def _noise_mode_changed(self, row, _property):
        if self._noise_change or self._applying_state:
            return
        mode = row.get_selected()
        value = [1, 2, 3, 4][self._anc_level.get_selected()] if mode == 0 else (7 if mode == 1 else 5)
        self._send("anc", value)

    def _anc_level_changed(self, _row, _property):
        if self._applying_state:
            return
        self._noise_change = True
        try:
            self._noise_mode.set_selected(0)
        finally:
            self._noise_change = False
        self._update_headline()
        self._send("anc", [1, 2, 3, 4][self._anc_level.get_selected()])

    def _custom_eq_changed(self, _row, _property):
        if self._applying_state:
            return
        if self._eq_timer is not None:
            GLib.Source.remove(self._eq_timer)
        self._eq_timer = GLib.timeout_add(350, self._send_custom_eq)

    def _send_custom_eq(self):
        self._eq_timer = None
        self._send("custom_eq", {name: row.get_value() for name, row in self._eq_rows.items()})
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _combo(group, title, options, selected=0, subtitle=""):
        row = Adw.ComboRow(
            title=title,
            subtitle=subtitle,
            model=Gtk.StringList.new(options),
            selected=selected,
        )
        group.add(row)
        return row

    @staticmethod
    def _switch(group, title, active=False, subtitle=""):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
        group.add(row)
        return row

    @staticmethod
    def _plain_row(group, title, subtitle=""):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        group.add(row)
        return row

    def _hero(self, page, kicker, heading):
        group = self._group(page, "")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("headphone-hero")
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)

        picture = Gtk.Picture.new_for_filename(
            str(HERE / "headphones.png")
        )
        self._hero_image = picture
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        image = Adw.Clamp.new()
        image.set_maximum_size(240)
        image.set_tightening_threshold(240)
        image.set_child(picture)
        image_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        image_stack.append(image)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, 16)
        image_stack.append(spacer)
        overlay = Gtk.Overlay()
        overlay.set_child(image_stack)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_halign(Gtk.Align.CENTER)
        text.set_valign(Gtk.Align.END)
        top = Gtk.Label(label=kicker, xalign=0.5)
        self._hero_status = top
        top.add_css_class("headphone-status")
        text.append(top)
        title = Gtk.Label(label=heading, xalign=0.5)
        self._hero_mode = title
        title.add_css_class("headphone-mode")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        text.append(title)
        overlay.add_overlay(text)
        overlay.set_measure_overlay(text, False)
        box.append(overlay)
        group.add(box)
        return title

    def _build_noise_page(self):
        page = self._page("Noise", "audio-headphones-symbolic")
        headline = self._hero(page, "Connected · 40%", "Adaptive ANC")
        group = self._group(page, "Noise control")
        group.set_margin_bottom(24)
        mode = self._combo(group, "Mode", ["ANC", "Transparency", "Off"])
        level = self._combo(group, "ANC level", ["High", "Mid", "Low", "Adaptive"], 3)
        self._noise_mode = mode
        self._anc_level = level

        def update_headline(*_args):
            choice = mode.get_selected()
            headline.set_text(
                f"{['High', 'Mid', 'Low', 'Adaptive'][level.get_selected()]} ANC"
                if choice == 0 else ("Transparency" if choice == 1 else "Noise control off")
            )

        mode.connect("notify::selected", update_headline)
        self._update_headline = update_headline

    @staticmethod
    def _eq_row(group, title, value):
        row = Adw.SpinRow.new_with_range(-6, 6, 1)
        row.set_title(title)
        row.set_value(value)
        row.set_numeric(True)
        row.set_snap_to_ticks(True)
        group.add(row)
        return row

    def _build_sound_page(self):
        page = self._page("Sound", "audio-speakers-symbolic")
        spatial = self._group(page, "Spatial audio")
        self._spatial = self._combo(spatial, "Mode", ["Off", "Concert", "Cinema"])

        eq = self._group(page, "Equaliser")
        preset = self._combo(
            eq, "Preset", ["Pop", "Rock", "Electronic", "Enhance Vocals", "Classical", "Custom"], 1,
        )
        self._eq_preset = preset
        custom = self._group(page, "Custom equaliser")
        self._eq_rows = {
            "bass": self._eq_row(custom, "Bass", -4),
            "mid": self._eq_row(custom, "Mid", 2),
            "treble": self._eq_row(custom, "Treble", 6),
        }
        custom.set_visible(False)
        preset.connect("notify::selected", lambda row, _prop: custom.set_visible(row.get_selected() == 5))

        playback = self._group(page, "Playback")
        self._personal_sound = self._switch(playback, "Personal Sound Profile")
        self._low_lag = self._switch(playback, "Low Lag Mode")

    def _build_controls_page(self):
        page = self._page("Controls", "input-gaming-symbolic")
        button = self._group(page, "Button")
        actions = ["No action", "Noise Control", "Voice Assistant", "Spatial Audio", "Mic On/Off"]
        single = self._combo(button, "Single press", actions, 3)
        hold = self._combo(button, "Press and hold", actions, 3)
        self._button_single = single
        self._button_hold = hold
        single_cycle = self._physical_cycle(button, "Single press noise cycle", "button_single")
        hold_cycle = self._physical_cycle(button, "Hold noise cycle", "button_hold")
        single_cycle.set_visible(False)
        hold_cycle.set_visible(False)
        single.connect("notify::selected", lambda row, _prop:
                       single_cycle.set_visible(row.get_selected() == 1))
        hold.connect("notify::selected", lambda row, _prop:
                     hold_cycle.set_visible(row.get_selected() == 1))
        slider = self._group(page, "Energy slider")
        self._slider_tuning = self._combo(slider, "Tuning", ["Bass", "Treble"], 1)

        roller = self._group(page, "Roller")
        roller_hold = self._combo(roller, "Press and hold", ["No action", "Noise Control"], 1)
        self._roller_hold = roller_hold
        roller_cycle = self._physical_cycle(roller, "Noise cycle", "roller_hold")
        roller_hold.connect("notify::selected", lambda row, _prop:
                            roller_cycle.set_visible(row.get_selected() == 1))
        fixed = self._group(page, "Other roller actions")
        for title, subtitle in (
            ("Single press", "Play/pause · answer/hang up calls"),
            ("Double press", "Next track"),
            ("Triple press", "Previous track"),
            ("Rotate", "Volume up/down"),
        ):
            self._plain_row(fixed, title, subtitle)

        # Device settings live with the physical controls.
        self._build_device_page(page)

    def _physical_cycle(self, group, title, key):
        expander = Adw.ExpanderRow(title=title)
        rows = {}
        for choice in ("ANC", "Transparency", "Off"):
            row = Adw.SwitchRow(title=choice, active=True)
            row.connect("notify::active", lambda widget, _prop, item=key:
                        self._physical_cycle_changed(item, widget))
            expander.add_row(row)
            rows[choice] = row
        group.add(expander)
        self._physical_cycles[key] = (expander, rows)
        return expander

    def _build_device_page(self, page):
        connection = self._group(page, "Connection")
        self._connection_row = self._plain_row(connection, "CMF Headphone Pro", "Connected · 40%")
        self._dual_connection = self._switch(connection, "Dual Connection", True)

        power = self._group(page, "Power")
        self._standby = self._combo(
            power, "Standby ANC",
            ["30 minutes", "1 hour", "2 hours", "3 hours", "4 hours"], 0,
        )

        find = self._group(page, "Find my headphones")
        row = Adw.ActionRow(
            title="Play a locating sound",
            subtitle="Stops after 10 seconds",
        )
        button = Gtk.Button(label="Start sound", valign=Gtk.Align.CENTER)
        timer = {"id": None}

        def clicked(_button):
            if timer["id"] is not None:
                GLib.Source.remove(timer["id"])
                timer["id"] = None
                button.set_label("Start sound")
                self._send("ring", False)
                return

            button.set_label("Stop sound")
            self._send("ring", True)

            def reset_button():
                timer["id"] = None
                button.set_label("Start sound")
                self._send("ring", False)
                return GLib.SOURCE_REMOVE

            timer["id"] = GLib.timeout_add_seconds(10, reset_button)

        button.connect("clicked", clicked)
        row.add_suffix(button)
        find.add(row)

        about = self._group(page, "About")
        self._plain_row(about, "Model", "CMF Headphone Pro · B175")

    def _build_extension_page(self):
        page = self._page("Extension", "application-x-addon-symbolic")
        quick = self._group(page, "Quick Settings", device_dependent=False)
        self._quick_enabled = self._switch(
            quick, "Quick tile", self._quick_config["enabled"],
        )
        self._quick_enabled.connect("notify::active", self._quick_setting_changed)

        self._quick_behavior = self._group(page, "Tile click", device_dependent=False)
        self._tile_click_action = self._combo(self._quick_behavior, "Action", ["Noise control"])
        self._tile_click_action.connect("notify::selected", self._tile_click_changed)

        self._quick_menu_group = self._group(page, "Menu", device_dependent=False)
        self._menu_list = Gtk.ListBox()
        self._menu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._menu_list.add_css_class("boxed-list")
        self._quick_menu_group.add(self._menu_list)
        self._rebuild_menu_rows()
        self._sync_click_choices()
        self._sync_quick_visibility()

    def _quick_setting_changed(self, row, _property):
        self._quick_config["enabled"] = row.get_active()
        self._sync_quick_visibility()
        save_quick_config(self._quick_config)

    def _sync_quick_visibility(self):
        enabled = self._quick_config["enabled"]
        self._quick_behavior.set_visible(enabled)
        self._quick_menu_group.set_visible(enabled)

    def _sync_click_choices(self):
        active = [key for key in self._quick_config["order"]
                  if self._quick_config["items"][key]["enabled"]]
        self._editing_quick_config = True
        try:
            self._tile_click_action.set_model(Gtk.StringList.new(
                [QUICK_ITEMS[key][0] for key in active] or ["No action"]
            ))
            if self._quick_config["click"] not in active:
                self._quick_config["click"] = active[0] if active else ""
            self._tile_click_action.set_selected(
                active.index(self._quick_config["click"]) if active else 0
            )
            self._tile_click_action.set_sensitive(bool(active))
        finally:
            self._editing_quick_config = False
        save_quick_config(self._quick_config)

    def _tile_click_changed(self, row, _property):
        if self._editing_quick_config:
            return
        active = [key for key in self._quick_config["order"]
                  if self._quick_config["items"][key]["enabled"]]
        if active:
            self._quick_config["click"] = active[row.get_selected()]
            save_quick_config(self._quick_config)

    def _menu_item_toggled(self, key, enabled):
        self._quick_config["items"][key]["enabled"] = enabled
        self._sync_click_choices()

    def _cycle_option_toggled(self, key, option, switch):
        options = self._quick_config["items"][key]["options"]
        if switch.get_active():
            if option not in options:
                options.append(option)
        elif option in options:
            minimum = 1 if key == "profile" else 2
            if len(options) <= minimum:
                switch.set_active(True)
                self._toast(f"Keep at least {minimum} value{'s' if minimum > 1 else ''}")
                return
            options.remove(option)
        choices = QUICK_ITEMS[key][1]
        options.sort(key=choices.index)
        save_quick_config(self._quick_config)

    def _rebuild_menu_rows(self):
        child = self._menu_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._menu_list.remove(child)
            child = next_child

        for key in self._quick_config["order"]:
            title, options = QUICK_ITEMS[key]
            if key in {"lag", "find"}:
                row = Adw.ActionRow(title=title)
                switch = Gtk.Switch(valign=Gtk.Align.CENTER)
                switch.set_active(self._quick_config["items"][key]["enabled"])
                switch.connect("notify::active", lambda widget, _prop, item=key:
                               self._menu_item_toggled(item, widget.get_active()))
                row.add_suffix(switch)
                row.set_activatable_widget(switch)
            else:
                row = Adw.ExpanderRow(title=title)
                row.set_show_enable_switch(True)
                row.set_enable_expansion(self._quick_config["items"][key]["enabled"])
                row.connect("notify::enable-expansion", lambda widget, _prop, item=key:
                            self._menu_item_toggled(item, widget.get_enable_expansion()))
                for option in options:
                    option_row = Adw.SwitchRow(
                        title=option,
                        active=option in self._quick_config["items"][key]["options"],
                    )
                    option_row.connect("notify::active", lambda widget, _prop, item=key, choice=option:
                                       self._cycle_option_toggled(item, choice, widget))
                    row.add_row(option_row)

            grip = Gtk.Box()
            grip.set_tooltip_text(f"Drag to move {title}")
            grip.set_margin_end(4)
            grip.append(Gtk.Image.new_from_icon_name("list-drag-handle-symbolic"))
            source = Gtk.DragSource.new()
            source.set_actions(Gdk.DragAction.MOVE)
            source.connect("prepare", lambda _source, _x, _y, item=key:
                           Gdk.ContentProvider.new_for_value(item))
            grip.add_controller(source)
            row.add_prefix(grip)

            target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
            target.connect("drop", lambda _target, value, _x, _y, item=key:
                           self._drop_menu_item(value, item))
            row.add_controller(target)
            self._menu_list.append(row)

    def _drop_menu_item(self, source, target):
        order = self._quick_config["order"]
        if source not in order or target not in order or source == target:
            return False
        order.remove(source)
        order.insert(order.index(target), source)
        self._rebuild_menu_rows()
        self._sync_click_choices()
        return True


class ControlsApp(Adw.Application):
    def __init__(self, disconnected=False):
        super().__init__(application_id="io.github.cmfheadphonepro.Controls", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._disconnected = disconnected

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = ControlsWindow(self, self._disconnected)
        window.present()
        if not self._disconnected:
            window.refresh_state()


if __name__ == "__main__":
    raise SystemExit(ControlsApp("--disconnected" in sys.argv).run([]))
