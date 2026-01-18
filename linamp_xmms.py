#!/usr/bin/env python3
from ast import Pass
import os
import sys
import json
import tempfile
import random
import time
from dataclasses import dataclass, asdict
import gi

try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gst', '1.0')
    from gi.repository import Gtk, Gio, GLib, Gst, Gdk, GObject, Pango
except ValueError:
    sys.exit(1)

from typing import Dict, Any, List, Optional, Union, Tuple
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "linamp_settings.json"

@dataclass
class PlaylistItem:
    path: str
    title: str = ""
    duration: int = 0

    def __post_init__(self):
        self.path = os.path.abspath(os.path.expanduser(str(self.path)))
        if not self.title:
            self.title = os.path.basename(self.path)
        if hasattr(self, 'filename') and not hasattr(self, 'path'):
            self.path = os.path.abspath(os.path.expanduser(str(self.filename)))
            delattr(self, 'filename')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'path': self.path,
            'title': self.title,
            'duration': self.duration
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PlaylistItem':
        if not isinstance(data, dict):
            raise ValueError("Input data must be a dictionary")
        item_data = data.copy()
        if 'filename' in item_data and 'path' not in item_data:
            item_data['path'] = item_data.pop('filename')
        if 'path' not in item_data:
            raise ValueError("Playlist item must contain a 'path' or 'filename' field")
        return cls(**item_data)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def get_display_name(self) -> str:
        return self.title if self.title else os.path.basename(self.path)

@dataclass
class PlayerSettings:
    auto_play_next: bool = True
    shuffle_mode: bool = False
    repeat_mode: str = "none"
    crossfade_enabled: bool = False
    crossfade_duration: float = 3.0
    beat_aware_enabled: bool = False
    beat_threshold: float = 0.1
    volume: float = 1.0
    position: float = 0.0
    window_size: Tuple[int, int] = (500, 400)
    window_position: Tuple[int, int] = (100, 100)
    equalizer_settings: List[float] = None
    last_played_track: str = ""
    last_played_position: float = 0.0

    def __post_init__(self):
        if self.equalizer_settings is None:
            self.equalizer_settings = [0.0] * 10

    def to_dict(self) -> Dict[str, Any]:
        return {
            'auto_play_next': self.auto_play_next,
            'shuffle_mode': self.shuffle_mode,
            'repeat_mode': self.repeat_mode,
            'crossfade_enabled': self.crossfade_enabled,
            'crossfade_duration': self.crossfade_duration,
            'beat_aware_enabled': self.beat_aware_enabled,
            'beat_threshold': self.beat_threshold,
            'volume': self.volume,
            'position': self.position,
            'window_size': self.window_size,
            'window_position': self.window_position,
            'equalizer_settings': self.equalizer_settings,
            'last_played_track': self.last_played_track,
            'last_played_position': self.last_played_position
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PlayerSettings':
        if not isinstance(data, dict):
            raise ValueError("Input data must be a dictionary")
        settings = cls(
            auto_play_next=data.get('auto_play_next', True),
            shuffle_mode=data.get('shuffle_mode', False),
            repeat_mode=data.get('repeat_mode', 'none'),
            crossfade_enabled=data.get('crossfade_enabled', False),
            crossfade_duration=data.get('crossfade_duration', 3.0),
            beat_aware_enabled=data.get('beat_aware_enabled', False),
            beat_threshold=data.get('beat_threshold', 0.1),
            volume=data.get('volume', 1.0),
            position=data.get('position', 0.0),
            window_size=tuple(data.get('window_size', (500, 400))),
            window_position=tuple(data.get('window_position', (100, 100))),
            equalizer_settings=data.get('equalizer_settings', [0.0] * 10),
            last_played_track=data.get('last_played_track', ''),
            last_played_position=data.get('last_played_position', 0.0)
        )
        return settings

class EqualizerTab(Gtk.Box):
    def __init__(self, player):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.player = player
        self.add_css_class("eq-tab")
        self.bands = 10
        self.band_scales = []
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main_box.set_margin_top(16)
        main_box.set_margin_bottom(16)
        main_box.set_margin_start(16)
        main_box.set_margin_end(16)
        self.append(main_box)
        header_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main_box.append(header_section)
        header_label = Gtk.Label(label="10-Band Equalizer")
        header_label.add_css_class("section-header")
        header_label.set_halign(Gtk.Align.CENTER)
        header_section.append(header_label)
        bands_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.append(bands_container)
        freq_labels_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        freq_labels_row.set_halign(Gtk.Align.CENTER)
        bands_container.append(freq_labels_row)
        freqs = ["60", "170", "310", "600", "1k", "3k", "6k", "12k", "14k", "16k"]
        for freq in freqs:
            freq_label = Gtk.Label(label=freq)
            freq_label.add_css_class("eq-label")
            freq_label.set_size_request(35, -1)
            freq_label.set_halign(Gtk.Align.CENTER)
            freq_labels_row.append(freq_label)
        sliders_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sliders_row.set_halign(Gtk.Align.CENTER)
        bands_container.append(sliders_row)
        for i in range(self.bands):
            band_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            band_container.add_css_class("eq-band")
            band_container.set_size_request(50, 180)
            value_label = Gtk.Label(label="0 dB")
            value_label.add_css_class("eq-value-label")
            value_label.set_size_request(40, -1)
            value_label.set_halign(Gtk.Align.CENTER)
            band_container.append(value_label)
            lower, upper = -12, 12
            value = max(lower, min(upper, 0))
            adjustment = Gtk.Adjustment(
                value=value, lower=lower, upper=upper, step_increment=0.1,
                page_increment=1, page_size=0
            )
            scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=adjustment)
            scale.set_inverted(True)
            scale.set_draw_value(False)
            scale.set_size_request(40, 140)
            scale.set_margin_top(4)
            scale.set_margin_bottom(4)
            scale.add_css_class("eq-scale")
            scale.connect("value-changed", self.on_band_changed, i, value_label)
            scale.add_mark(0, Gtk.PositionType.LEFT, "0")
            scale.add_mark(-12, Gtk.PositionType.LEFT, "-12")
            scale.add_mark(12, Gtk.PositionType.LEFT, "+12")
            band_container.append(scale)
            sliders_row.append(band_container)
            self.band_scales.append((scale, value_label))
        presets_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.append(presets_section)
        presets_header = Gtk.Label(label="Presets")
        presets_header.add_css_class("section-header")
        presets_header.set_halign(Gtk.Align.START)
        presets_section.append(presets_header)
        presets_grid = Gtk.Grid()
        presets_grid.set_column_spacing(8)
        presets_grid.set_row_spacing(8)
        presets_grid.set_halign(Gtk.Align.CENTER)
        presets_section.append(presets_grid)
        presets = [
            ("Flat", 0, 0), ("Pop", 1, 0), ("Rock", 2, 0), ("Jazz", 3, 0),
            ("Classical", 0, 1), ("Electronic", 1, 1), ("Hip-Hop", 2, 1), ("Metal", 3, 1),
            ("Acoustic", 0, 2), ("Vocal", 1, 2), ("Bass Boost", 2, 2), ("Treble Boost", 3, 2)
        ]
        for preset_name, col, row in presets:
            btn = self._create_preset_button(preset_name)
            btn.connect("clicked", self.on_preset_clicked, preset_name)
            presets_grid.attach(btn, col, row, 1, 1)
        reset_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        reset_container.set_halign(Gtk.Align.CENTER)
        reset_container.set_margin_top(12)
        presets_section.append(reset_container)
        reset_btn = self._create_preset_button("Reset to Flat")
        reset_btn.add_css_class("reset-button")
        reset_btn.connect("clicked", self.on_reset_clicked)
        reset_container.append(reset_btn)

    def _create_preset_button(self, text):
        button = Gtk.Button(label=text)
        button.add_css_class("eq-preset-btn")
        button.set_size_request(80, 28)
        return button

    def on_band_changed(self, scale, band, value_label):
        if hasattr(self.player, 'equalizer'):
            value = scale.get_value()
            clamped_value = max(-12, min(12, value))
            if clamped_value != value:
                scale.set_value(clamped_value)
                value = clamped_value
            self.player.equalizer.set_property(f'band{band}', value)
            value_label.set_text(f"{value:+.1f} dB")
            if hasattr(self.player, 'auto_save_settings'):
                self.player.auto_save_settings()

    def on_reset_clicked(self, button):
        for scale, value_label in self.band_scales:
            scale.set_value(0)
        if hasattr(self.player, 'auto_save_settings'):
            self.player.auto_save_settings()

    def on_preset_clicked(self, button, preset_name):
        presets = {
            "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            "Pop": [4, 3, 2, 1, 0, -1, -2, -2, -2, -2],
            "Rock": [6, 4, 2, 0, -2, -3, -3, -3, -3, -3],
            "Jazz": [3, 2, 1, 1, 0, -1, -2, -2, -2, -2],
            "Classical": [4, 3, 2, 1, 0, -1, -2, -3, -4, -5],
            "Electronic": [5, 4, 3, 1, 0, 1, 2, 3, 4, 5],
            "Hip-Hop": [5, 4, 2, 0, -1, 1, 3, 4, 5, 5],
            "Metal": [7, 6, 5, 3, 1, -1, -2, -3, -3, -3],
            "Acoustic": [-2, -1, 0, 2, 4, 4, 3, 2, 1, 0],
            "Vocal": [-1, 0, 2, 3, 4, 3, 2, 1, 0, -1],
            "Bass Boost": [8, 7, 6, 4, 2, 0, -1, -2, -3, -4],
            "Treble Boost": [-4, -3, -2, 0, 2, 4, 6, 7, 8, 8],
        }
        if preset_name in presets:
            values = presets[preset_name]
            for i, (scale, value_label) in enumerate(self.band_scales):
                scale.set_value(values[i])
            if hasattr(self.player, 'auto_save_settings'):
                self.player.auto_save_settings()

class PlaylistTab(Gtk.Box):
    def __init__(self, player):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.player = player
        self.add_css_class("playlist-tab")
        self.playlist_store = Gtk.StringList()
        self.filtered_store = None
        self.filter_model = None
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(16)
        main_box.set_margin_bottom(16)
        main_box.set_margin_start(16)
        main_box.set_margin_end(16)
        self.append(main_box)
        search_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main_box.append(search_section)
        search_header = Gtk.Label(label="Search Playlist")
        search_header.add_css_class("section-header")
        search_header.set_halign(Gtk.Align.START)
        search_section.append(search_header)
        search_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_section.append(search_container)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Type to filter playlist...")
        self.search_entry.set_hexpand(True)
        self.search_entry.add_css_class("search-entry")
        self.search_entry.connect("changed", self.on_search_changed)
        search_container.append(self.search_entry)
        clear_search_btn = self._create_modern_button("Clear", "edit-clear-symbolic")
        clear_search_btn.connect("clicked", self.on_clear_search)
        search_container.append(clear_search_btn)
        toolbar_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main_box.append(toolbar_section)
        toolbar_header = Gtk.Label(label="Playlist Actions")
        toolbar_header.add_css_class("section-header")
        toolbar_header.set_halign(Gtk.Align.START)
        toolbar_section.append(toolbar_header)
        main_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        main_toolbar.set_homogeneous(True)
        toolbar_section.append(main_toolbar)
        add_btn = self._create_modern_button("Add Files", "list-add-symbolic")
        add_btn.connect("clicked", self.on_add_files)
        main_toolbar.append(add_btn)
        add_folder_btn = self._create_modern_button("Add Folder", "folder-open-symbolic")
        add_folder_btn.connect("clicked", self.on_add_folder)
        main_toolbar.append(add_folder_btn)
        remove_btn = self._create_modern_button("Remove", "list-remove-symbolic")
        remove_btn.connect("clicked", self.on_remove)
        main_toolbar.append(remove_btn)
        clear_btn = self._create_modern_button("Clear All", "edit-delete-symbolic")
        clear_btn.connect("clicked", self.on_clear)
        main_toolbar.append(clear_btn)
        shuffle_btn = self._create_modern_button("Shuffle", "media-playlist-shuffle-symbolic")
        shuffle_btn.connect("clicked", self.on_shuffle)
        main_toolbar.append(shuffle_btn)
        secondary_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        secondary_toolbar.set_homogeneous(True)
        toolbar_section.append(secondary_toolbar)
        sort_dropdown = Gtk.DropDown.new_from_strings(["Default Order", "By Title", "By Path", "By Duration"])
        sort_dropdown.add_css_class("sort-dropdown")
        sort_dropdown.connect("notify::selected", self.on_sort_changed)
        secondary_toolbar.append(sort_dropdown)
        self.sort_dropdown = sort_dropdown
        import_btn = self._create_modern_button("Import", "document-open-symbolic")
        import_btn.connect("clicked", self.on_import_m3u)
        secondary_toolbar.append(import_btn)
        export_btn = self._create_modern_button("Export", "document-save-symbolic")
        export_btn.connect("clicked", self.on_export_m3u)
        secondary_toolbar.append(export_btn)
        remove_dups_btn = self._create_modern_button("Remove Dups", "view-refresh-symbolic")
        remove_dups_btn.connect("clicked", self.on_remove_duplicates)
        secondary_toolbar.append(remove_dups_btn)
        stats_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        stats_container.set_halign(Gtk.Align.END)
        stats_container.set_margin_top(8)
        toolbar_section.append(stats_container)
        self.stats_label = Gtk.Label(label="0 tracks")
        self.stats_label.add_css_class("stats-label")
        stats_container.append(self.stats_label)
        playlist_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        playlist_container.set_margin_top(12)
        main_box.append(playlist_container)
        playlist_header = Gtk.Label(label="Track List")
        playlist_header.add_css_class("section-header")
        playlist_header.set_halign(Gtk.Align.START)
        playlist_container.append(playlist_header)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC
        )
        scrolled.add_css_class("playlist-scrolled")
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        playlist_container.append(scrolled)
        self.filter_model = Gtk.FilterListModel()
        self.filter_model.set_model(self.playlist_store)
        self.selection_model = Gtk.SingleSelection(model=self.filter_model)
        self.column_view = Gtk.ColumnView(model=self.selection_model)
        self.column_view.add_css_class("playlist-view")
        self.column_view.set_hexpand(True)
        self.column_view.set_vexpand(True)
        self.column_view.set_size_request(-1, 180)
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_factory_setup)
        factory.connect('bind', self._on_factory_bind)
        column = Gtk.ColumnViewColumn(title="Tracks", factory=factory)
        self.column_view.append_column(column)
        scrolled.set_child(self.column_view)
        self.selection_model.connect("selection-changed", self.on_selection_changed)
        self.column_view.connect("activate", self.on_row_activated)

    def _create_modern_button(self, text, icon_name=None):
        button = Gtk.Button()
        if icon_name:
            icon_fallback_chains = {
                "list-add-symbolic": [
                    "list-add-symbolic",
                    "list-add",
                    "add-symbolic",
                    "add",
                    "gtk-add",
                    "insertion-symbolic",
                    "plus-symbolic",
                    "plus"
                ],
                "folder-open-symbolic": [
                    "folder-open-symbolic",
                    "folder-open",
                    "folder-symbolic",
                    "folder",
                    "directory-symbolic",
                    "directory",
                    "open-folder-symbolic",
                    "open-folder"
                ],
                "list-remove-symbolic": [
                    "list-remove-symbolic",
                    "list-remove",
                    "remove-symbolic",
                    "remove",
                    "gtk-remove",
                    "removal-symbolic",
                    "minus-symbolic",
                    "minus"
                ],
                "edit-delete-symbolic": [
                    "edit-delete-symbolic",
                    "edit-delete",
                    "delete-symbolic",
                    "delete",
                    "gtk-delete",
                    "trash-symbolic",
                    "trash",
                    "wastebasket-symbolic",
                    "wastebasket"
                ],
                "media-playlist-shuffle-symbolic": [
                    "media-playlist-shuffle-symbolic",
                    "media-playlist-shuffle",
                    "shuffle-symbolic",
                    "shuffle",
                    "random-symbolic",
                    "random"
                ],
                "document-open-symbolic": [
                    "document-open-symbolic",
                    "document-open",
                    "open-symbolic",
                    "open",
                    "file-open-symbolic",
                    "file-open",
                    "folder-symbolic",
                    "folder"
                ],
                "document-save-symbolic": [
                    "document-save-symbolic",
                    "document-save",
                    "save-symbolic",
                    "save",
                    "file-save-symbolic",
                    "file-save",
                    "gtk-save"
                ],
                "edit-duplicate-symbolic": [
                    "edit-duplicate-symbolic",
                    "edit-duplicate",
                    "duplicate-symbolic",
                    "duplicate",
                    "copy-symbolic",
                    "copy",
                    "gtk-copy"
                ],
                "edit-clear-symbolic": [
                    "edit-clear-symbolic",
                    "edit-clear",
                    "clear-symbolic",
                    "clear",
                    "gtk-clear",
                    "remove-symbolic",
                    "remove"
                ],
                "view-pulse-symbolic": [
                    "view-pulse-symbolic",
                    "view-pulse",
                    "pulse-symbolic",
                    "pulse",
                    "audio-x-generic-symbolic",
                    "audio-x-generic",
                    "multimedia-symbolic",
                    "multimedia"
                ],
            }
            fallbacks = icon_fallback_chains.get(icon_name, [
                icon_name,
                icon_name.replace("-symbolic", ""),
            ])
            icon_set = False
            for fallback in fallbacks:
                try:
                    content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    icon = Gtk.Image.new_from_icon_name(fallback)
                    icon.set_pixel_size(14)
                    content_box.append(icon)
                    label = Gtk.Label(label=text)
                    content_box.append(label)
                    button.set_child(content_box)
                    icon_set = True
                    break
                except Exception:
                       Pass
            if not icon_set:
                button.set_label(self._get_emoji_fallback(text))
        else:
            button.set_label(text)
        return button

    def _get_emoji_fallback(self, text):
        emoji_fallbacks = {
            "Add Files": "📁+",
            "Add Folder": "📂",
            "Remove": "❌",
            "Clear All": "🗑️",
            "Shuffle": "🔀",
            "Import": "📥",
            "Export": "📤",
            "Remove Dups": "🔄",
            "Clear": "🧹",
        }
        return emoji_fallbacks.get(text, text)

    def on_add_files(self, button):
        dialog = Gtk.FileChooserNative(
            title="Add Files",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="_Open",
            cancel_label="_Cancel"
        )
        dialog.set_select_multiple(True)
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        audio_filter.add_mime_type("audio/*")
        dialog.add_filter(audio_filter)
        dialog.connect("response", self.on_files_selected)
        dialog.set_modal(True)
        dialog.show()

    def on_files_selected(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            files = dialog.get_files()
            file_paths = [file.get_path() for file in files if file.get_path() is not None]
            self.player.add_to_playlist(file_paths)
        dialog.destroy()

    def on_add_folder(self, button):
        dialog = Gtk.FileChooserNative(
            title="Add Music Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="_Add",
            cancel_label="_Cancel"
        )
        dialog.connect("response", self.on_folder_selected)
        dialog.set_modal(True)
        dialog.show()

    def on_folder_selected(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file()
            if folder:
                folder_path = folder.get_path()
                self.player.add_folder_to_playlist(folder_path)
        dialog.destroy()

    def on_remove(self, button):
        position = self.selection_model.get_selected()
        if position != Gtk.INVALID_LIST_POSITION:
            if position < len(self.playlist_store):
                self.playlist_store.remove(position)
                if position < len(self.player.playlist):
                    self.player.playlist.pop(position)

    def on_clear(self, button):
        while self.playlist_store.get_n_items() > 0:
            self.playlist_store.remove(0)
        self.player.playlist.clear()
        self.update_statistics()

    def _on_factory_setup(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        list_item.set_child(label)

    def _on_factory_bind(self, factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        if item is not None:
            text = item.get_string() if hasattr(item, 'get_string') else str(item)
            label.set_text(text)

    def on_selection_changed(self, selection, position, n_items):
        pass

    def on_search_changed(self, entry):
        search_text = entry.get_text().lower()
        if not search_text:
            self.filter_model.set_filter(None)
        else:
            filter_obj = Gtk.CustomFilter()
            filter_obj.set_filter_func(lambda item: self._filter_func(item, search_text))
            self.filter_model.set_filter(filter_obj)

    def _filter_func(self, item, search_text):
        if item is None:
            return False
        text = item.get_string().lower()
        return search_text in text

    def on_clear_search(self, button):
        self.search_entry.set_text("")
        self.filter_model.set_filter(None)

    def on_shuffle(self, button):
        if self.player.playlist:
            import random
            random.shuffle(self.player.playlist)
            self.player._update_playlist_display()
            self.player.save_playlist()

    def on_sort_changed(self, dropdown, pspec):
        selected = dropdown.get_selected()
        if selected == 0:
            pass
        elif selected == 1:
            self.player.playlist.sort(key=lambda x: x.title.lower())
        elif selected == 2:
            self.player.playlist.sort(key=lambda x: x.path.lower())
        elif selected == 3:
            self.player.playlist.sort(key=lambda x: x.duration)
        if selected != 0:
            self.player._update_playlist_display()
            self.player.save_playlist()

    def update_statistics(self):
        total_tracks = len(self.player.playlist)
        total_duration = sum(item.duration for item in self.player.playlist if item.duration > 0)
        if total_duration > 0:
            hours = total_duration // 3600
            minutes = (total_duration % 3600) // 60
            if hours > 0:
                duration_text = f"{hours}h {minutes}m"
            else:
                duration_text = f"{minutes}m"
            stats_text = f"{total_tracks} tracks • {duration_text}"
        else:
            stats_text = f"{total_tracks} tracks"
        self.stats_label.set_text(stats_text)

    def on_import_m3u(self, button):
        dialog = Gtk.FileChooserNative(
            title="Import M3U Playlist",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="_Import",
            cancel_label="_Cancel"
        )
        m3u_filter = Gtk.FileFilter()
        m3u_filter.set_name("M3U playlist files")
        m3u_filter.add_mime_type("audio/x-mpegurl")
        m3u_filter.add_pattern("*.m3u")
        m3u_filter.add_pattern("*.M3U")
        dialog.add_filter(m3u_filter)
        dialog.connect("response", self._on_m3u_import_selected)
        dialog.set_modal(True)
        dialog.show()

    def _on_m3u_import_selected(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                filepath = file.get_path()
                if filepath:
                    self._import_m3u_file(filepath)
        dialog.destroy()

    def _import_m3u_file(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            imported_files = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if not os.path.isabs(line):
                    m3u_dir = os.path.dirname(filepath)
                    line = os.path.join(m3u_dir, line)
                line = os.path.abspath(os.path.expanduser(line))
                if os.path.exists(line) and os.path.isfile(line):
                    imported_files.append(line)
            if imported_files:
                self.player.add_to_playlist(imported_files)
        except Exception:
            pass

    def on_export_m3u(self, button):
        if not self.player.playlist:
            return
        dialog = Gtk.FileChooserNative(
            title="Export M3U Playlist",
            action=Gtk.FileChooserAction.SAVE,
            accept_label="_Export",
            cancel_label="_Cancel"
        )
        dialog.set_current_name("playlist.m3u")
        m3u_filter = Gtk.FileFilter()
        m3u_filter.set_name("M3U playlist files")
        m3u_filter.add_mime_type("audio/x-mpegurl")
        m3u_filter.add_pattern("*.m3u")
        dialog.add_filter(m3u_filter)
        dialog.connect("response", self._on_m3u_export_selected)
        dialog.set_modal(True)
        dialog.show()

    def _on_m3u_export_selected(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                filepath = file.get_path()
                if filepath:
                    if not filepath.lower().endswith('.m3u'):
                        filepath += '.m3u'
                    self._export_m3u_file(filepath)
        dialog.destroy()

    def _export_m3u_file(self, filepath):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write("#EXTM3U\n")
                for item in self.player.playlist:
                    if hasattr(item, 'path') and os.path.exists(item.path):
                        if hasattr(item, 'duration') and item.duration > 0 and hasattr(item, 'title'):
                            f.write(f"#EXTINF:{item.duration},{item.title}\n")
                        f.write(f"{item.path}\n")
            pass
        except Exception:
            pass

    def on_remove_duplicates(self, button):
        if not self.player.playlist:
            return
        seen_paths = set()
        seen_titles = set()
        duplicates_removed = 0
        unique_playlist = []
        for item in self.player.playlist:
            if item.path in seen_paths:
                duplicates_removed += 1
                continue
            title_key = item.title.lower().strip()
            if title_key in seen_titles and title_key != "":
                duplicates_removed += 1
                continue
            seen_paths.add(item.path)
            seen_titles.add(title_key)
            unique_playlist.append(item)
        if duplicates_removed > 0:
            self.player.playlist = unique_playlist
            self.player._update_playlist_display()
            self.player.save_playlist()
            pass
        else:
            pass

    def on_row_activated(self, column_view, position):
        if hasattr(self, 'filter_model') and self.filter_model.get_filter():
            item = self.selection_model.get_item(position)
            if item is not None:
                track_title = item.get_string()
                for i, playlist_item in enumerate(self.player.playlist):
                    if playlist_item.title == track_title:
                        self.player.play_track(i)
                        break
        else:
            item = self.playlist_store.get_item(position)
            if item is not None:
                track_path = item.get_string()
                self.player.play_track(position)

class PlayerTab(Gtk.Box):
    def __init__(self, player):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.player = player
        self.add_css_class("player-tab")
        self.key_controller = Gtk.EventControllerKey.new()
        self.key_controller.connect('key-pressed', self.on_key_pressed)
        self.add_controller(self.key_controller)
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_margin_bottom(8)
        self.append(info_box)
        self.track_label = Gtk.Label(label="No track playing")
        self.track_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.track_label.add_css_class("track-label")
        info_box.append(self.track_label)
        self.time_label = Gtk.Label(label="--:-- / --:--")
        self.time_label.add_css_class("time-label")
        info_box.append(self.time_label)
        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_size_request(20, 20)
        self.progress.set_margin_start(10)
        self.progress.set_margin_end(10)
        self.progress.add_css_class("osd")
        self.progress_controller = Gtk.GestureClick()
        self.progress_controller.connect("pressed", self.on_progress_pressed)
        self.progress.add_controller(self.progress_controller)
        self.append(self.progress)
        controls_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls_container.set_margin_top(12)
        controls_container.set_margin_bottom(12)
        self.append(controls_container)
        self.prev_btn = self._create_icon_button("media-skip-backward-symbolic", "Previous (Ctrl+Left)")
        self.play_btn = self._create_icon_button("media-playback-start-symbolic", "Play (Space)")
        self.pause_btn = self._create_icon_button("media-playback-pause-symbolic", "Pause (Space)")
        self.stop_btn = self._create_icon_button("media-playback-stop-symbolic", "Stop (S)")
        self.next_btn = self._create_icon_button("media-skip-forward-symbolic", "Next (Ctrl+Right)")
        self.play_btn.add_css_class("suggested-action")
        self.repeat_btn = self._create_icon_button("media-playlist-repeat-symbolic", "Repeat")
        self.shuffle_btn = self._create_icon_button("media-playlist-shuffle-symbolic", "Shuffle")
        self.crossfade_btn = self._create_icon_button("media-playlist-consecutive-symbolic", "Crossfade")
        self.beat_btn = self._create_icon_button("process-working-symbolic", "Beat")
        self.autonext_btn = self._create_icon_button("go-next-symbolic", "Auto Next")
        main_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        main_controls.set_halign(Gtk.Align.CENTER)
        main_controls.set_homogeneous(True)
        for btn in [self.prev_btn, self.play_btn, self.pause_btn, self.stop_btn, self.next_btn]:
            btn.add_css_class("control-button")
            main_controls.append(btn)
        controls_container.append(main_controls)
        secondary_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        secondary_controls.set_halign(Gtk.Align.CENTER)
        for btn in [self.repeat_btn, self.shuffle_btn, self.crossfade_btn, self.beat_btn, self.autonext_btn]:
            btn.set_size_request(36, 32)
            secondary_controls.append(btn)
        controls_container.append(secondary_controls)
        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vol_box.set_margin_top(8)
        vol_box.set_halign(Gtk.Align.CENTER)
        self.append(vol_box)
        vol_icon = self._create_volume_icon()
        vol_icon.add_css_class("volume-icon")
        vol_box.append(vol_icon)
        lower, upper = 0, 100
        value = max(lower, min(upper, 70))
        vol_adjustment = Gtk.Adjustment(
            value=value, lower=lower, upper=upper, step_increment=1,
            page_increment=10, page_size=0
        )
        self.volume_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=vol_adjustment)
        self.volume_scale.set_hexpand(True)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(200, -1)
        vol_box.append(self.volume_scale)
        self.volume_label = Gtk.Label(label="70%")
        self.volume_label.add_css_class("volume-label")
        self.volume_label.set_size_request(40, -1)
        vol_box.append(self.volume_label)
        self.update_playback_state(False)

    def _create_volume_icon(self):
        icon_names = [
            "audio-volume-high-symbolic",
            "audio-volume-high",
            "stock_volume-100",
            "volume-high",
            "speaker"
        ]
        for icon_name in icon_names:
            try:
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.set_pixel_size(16)
                return icon
            except:
                continue
        label = Gtk.Label(label="🔊")
        return label

    def _create_icon_button(self, icon_name, tooltip_text):
        button = Gtk.Button()
        button.set_tooltip_text(tooltip_text)
        fallback_chains = {
            "media-skip-backward-symbolic": [
                "media-skip-backward-symbolic",
                "media-skip-backward",
                "media-seek-backward-symbolic",
                "media-seek-backward",
                "go-previous-symbolic",
                "go-previous",
                "skip-backward"
            ],
            "media-playback-start-symbolic": [
                "media-playback-start-symbolic",
                "media-playback-start",
                "media-play-symbolic",
                "media-play",
                "play-symbolic",
                "play"
            ],
            "media-playback-pause-symbolic": [
                "media-playback-pause-symbolic",
                "media-playback-pause",
                "media-pause-symbolic",
                "media-pause",
                "pause-symbolic",
                "pause"
            ],
            "media-playback-stop-symbolic": [
                "media-playback-stop-symbolic",
                "media-playback-stop",
                "media-stop-symbolic",
                "media-stop",
                "stop-symbolic",
                "stop"
            ],
            "media-skip-forward-symbolic": [
                "media-skip-forward-symbolic",
                "media-skip-forward",
                "media-seek-forward-symbolic",
                "media-seek-forward",
                "go-next-symbolic",
                "go-next",
                "skip-forward"
            ],
            "media-playlist-repeat-symbolic": [
                "media-playlist-repeat-symbolic",
                "media-playlist-repeat",
                "repeat-symbolic",
                "repeat",
                "media-repeat-symbolic",
                "media-repeat"
            ],
            "media-playlist-shuffle-symbolic": [
                "media-playlist-shuffle-symbolic",
                "media-playlist-shuffle",
                "shuffle-symbolic",
                "shuffle"
            ],
            "media-playlist-consecutive-symbolic": [
                "media-playlist-consecutive-symbolic",
                "media-playlist-consecutive",
                "media-playlist-repeat-symbolic",
                "media-playlist-repeat",
                "consecutive-repeat-symbolic",
                "consecutive-repeat"
            ],
            "view-pulse-symbolic": [
                "view-pulse-symbolic",
                "view-pulse",
                "pulse-symbolic",
                "pulse",
                "drum-symbolic",
                "drum",
                "audio-eq-symbolic",
                "audio-eq",
                "equalizer-symbolic",
                "equalizer",
                "multimedia-symbolic",
                "multimedia"
            ],
            "go-next-symbolic": [
                "go-next-symbolic",
                "go-next",
                "forward-symbolic",
                "forward",
                "media-skip-forward-symbolic",
                "media-skip-forward"
            ],
        }
        fallbacks = fallback_chains.get(icon_name, [
            icon_name,
            icon_name.replace("-symbolic", ""),
        ])
        icon_image = None
        for fallback in fallbacks:
            try:
                icon_image = Gtk.Image.new_from_icon_name(fallback)
                icon_image.set_pixel_size(16)
                button.set_child(icon_image)
                return button
            except:
                continue
        button.set_label(self._get_text_fallback(tooltip_text))
        return button

    def _get_text_fallback(self, tooltip_text):
        text_fallbacks = {
            "Previous": "⏮",
            "Play": "▶",
            "Pause": "⏸",
            "Stop": "⏹",
            "Next": "⏭",
            "Repeat": "🔁",
            "Shuffle": "🔀",
            "Crossfade": "↔",
            "Beat": "♪",
            "Auto Next": "⏭",
        }
        return text_fallbacks.get(tooltip_text, tooltip_text[0].upper())

    def on_key_pressed(self, controller, keyval, keycode, state):
        keyname = Gdk.keyval_name(keyval)
        ctrl = (state & Gdk.ModifierType.CONTROL_MASK)
        if keyname == 'space':
            if self.player.is_playing():
                self.player.pause()
            else:
                self.player.play()
            return True
        elif keyname == 'Left' and ctrl:
            self.player.on_prev(None)
            return True
        elif keyname == 'Right' and ctrl:
            self.player.on_next(None)
            return True
        elif keyname.lower() == 's':
            self.player.stop()
            return True
        return False

    def update_playback_state(self, is_playing):
        if is_playing:
            self.play_btn.set_visible(False)
            self.pause_btn.set_visible(True)
            self.add_css_class("playing")
        else:
            self.play_btn.set_visible(True)
            self.pause_btn.set_visible(False)
            self.remove_css_class("playing")

    def on_progress_pressed(self, gesture, n_press, x, y):
        width = self.progress.get_width()
        ratio = x / width if width > 0 else 0
        duration = self.player.player.query_duration(Gst.Format.TIME)[1]
        if duration > 0:
            seek_time = int(ratio * duration)
            self.player.player.seek(1.0, Gst.Format.TIME,
                                  Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                  Gst.SeekType.SET, seek_time,
                                  Gst.SeekType.NONE, 0)

    def update_track_info(self, title, artist="", album=""):
        if not title:
            title = "No track playing"
            artist = ""
            album = ""
        if artist and album:
            info_text = f"<b>{GLib.markup_escape_text(title)}</b>\n" \
                       f"<small>{GLib.markup_escape_text(artist)} • {GLib.markup_escape_text(album)}</small>"
        elif artist:
            info_text = f"<b>{GLib.markup_escape_text(title)}</b>\n" \
                       f"<small>{GLib.markup_escape_text(artist)}</small>"
        else:
            info_text = f"<b>{GLib.markup_escape_text(title)}</b>"
        self.track_label.set_markup(info_text)
        self.track_label.set_tooltip_text(f"{title}\n{artist}{' • ' + album if album else ''}")
        window = self.get_root()
        if window:
            window.set_title(f"{title} - LinAmp")

class WinampWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("LinAmp")
        self.set_default_size(650, 500)
        self.set_resizable(True)
        self.set_size_request(400, 300)
        self.connect("notify::default-width", self.on_window_size_changed)
        self.connect("notify::default-height", self.on_window_size_changed)
        self.connect("notify::width", self.on_window_size_changed)
        self.connect("notify::height", self.on_window_size_changed)
        self.apply_xmms_css()
        self.playlist = []
        self.current_track = -1
        self.shuffled_indices = []
        self.shuffle_position = 0
        self.settings = PlayerSettings()
        self.auto_play_next = self.settings.auto_play_next
        self.shuffle_mode = self.settings.shuffle_mode
        self.repeat_mode = self.settings.repeat_mode
        self.crossfade_enabled = self.settings.crossfade_enabled
        self.crossfade_duration = self.settings.crossfade_duration
        self.beat_aware_enabled = self.settings.beat_aware_enabled
        self.beat_detector = None
        self.beat_detection_timer = None
        self.last_beat_time = 0
        self.beat_threshold = 0.1
        self.beat_interval_history = []
        self.crossfade_player = None
        self.crossfade_volume = 1.0
        self.is_compact_mode = False
        self.current_window_width = 650
        Gst.init(None)
        self.load_settings()
        self.setup_player()
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_box.add_css_class("main-container")
        self.set_child(self.main_box)
        self.notebook = Gtk.Notebook()
        self.notebook.add_css_class("main-notebook")
        self.notebook.set_size_request(400, 300)

        def on_critical_log(log_domain, log_level, message, user_data=None):
            if "CRITICAL" in str(log_level):
                print("GTK CRITICAL:", message)
                import traceback
                traceback.print_stack()
            return
        GLib.log_set_handler("Gtk", GLib.LogLevelFlags.LEVEL_CRITICAL, on_critical_log, None)
        self.main_box.append(self.notebook)
        self.player_tab = PlayerTab(self)
        self.equalizer_tab = EqualizerTab(self)
        self.playlist_tab = PlaylistTab(self)
        self.notebook.append_page(self.player_tab, Gtk.Label(label="Player"))
        self.notebook.append_page(self.equalizer_tab, Gtk.Label(label="Equalizer"))
        self.notebook.append_page(self.playlist_tab, Gtk.Label(label="Playlist"))
        self.player_tab.play_btn.connect("clicked", self.on_play)
        self.player_tab.pause_btn.connect("clicked", self.on_pause)
        self.player_tab.stop_btn.connect("clicked", self.on_stop)
        self.player_tab.prev_btn.connect("clicked", self.on_prev)
        self.player_tab.next_btn.connect("clicked", self.on_next)
        self.player_tab.volume_scale.connect("value-changed", self.on_volume_changed)
        self.player_tab.repeat_btn.connect("clicked", lambda b: self.toggle_repeat_mode())
        self.player_tab.shuffle_btn.connect("clicked", lambda b: self.toggle_shuffle_mode())
        self.player_tab.crossfade_btn.connect("clicked", lambda b: self.toggle_crossfade())
        self.player_tab.beat_btn.connect("clicked", lambda b: self.toggle_beat_aware())
        self.player_tab.autonext_btn.connect("clicked", lambda b: self.toggle_auto_play_next())
        self.status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.status_bar.set_size_request(-1, 28)
        self.status_bar.add_css_class("statusbar")
        self.status_label = Gtk.Label()
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_hexpand(True)
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_end(12)
        self.status_bar.append(self.status_label)
        self.main_box.append(self.status_bar)
        self.setup_drag_drop()
        self.load_settings()
        GLib.timeout_add(500, self._apply_equalizer_settings_delayed)
        GLib.timeout_add(1000, self._apply_ui_settings_delayed)
        GLib.timeout_add(100, self.update_display)
        GLib.timeout_add(30000, self.periodic_auto_save)
        self.load_playlist()

    def on_window_size_changed(self, widget, pspec):
        width = widget.get_width()
        height = widget.get_height()
        if width <= 0:
            width = self.current_window_width
        was_compact = self.is_compact_mode
        self.is_compact_mode = width < 500
        if was_compact != self.is_compact_mode:
            self.apply_responsive_layout()
        self.current_window_width = width

    def apply_responsive_layout(self):
        if self.is_compact_mode:
            if hasattr(self.player_tab, 'volume_scale'):
                self.player_tab.volume_scale.set_size_request(150, -1)
            for btn in [self.player_tab.prev_btn, self.player_tab.play_btn,
                       self.player_tab.pause_btn, self.player_tab.stop_btn,
                       self.player_tab.next_btn]:
                btn.set_size_request(32, 28)
            if hasattr(self.playlist_tab, 'search_entry'):
                self.playlist_tab.search_entry.set_margin_start(8)
                self.playlist_tab.search_entry.set_margin_end(8)
        else:
            if hasattr(self.player_tab, 'volume_scale'):
                self.player_tab.volume_scale.set_size_request(200, -1)
            for btn in [self.player_tab.prev_btn, self.player_tab.play_btn,
                       self.player_tab.pause_btn, self.player_tab.stop_btn,
                       self.player_tab.next_btn]:
                btn.set_size_request(40, 36)
            if hasattr(self.playlist_tab, 'search_entry'):
                self.playlist_tab.search_entry.set_margin_start(16)
                self.playlist_tab.search_entry.set_margin_end(16)

    def set_status_message(self, message):
        self.status_label.set_label(message)

    def save_settings(self):
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(asdict(self.settings), f, indent=2)
            pass
        except Exception:
            pass

    def load_settings(self):
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r') as f:
                settings_dict = json.load(f)
            self.settings = PlayerSettings(**settings_dict)
            self._apply_settings_to_state()
            if hasattr(self.settings, 'last_played_track') and self.settings.last_played_track:
                self._resume_last_played()

    def _update_settings_from_state(self):
        self.settings.auto_play_next = self.auto_play_next
        self.settings.shuffle_mode = self.shuffle_mode
        self.settings.repeat_mode = self.repeat_mode
        self.settings.crossfade_enabled = self.crossfade_enabled
        self.settings.crossfade_duration = self.crossfade_duration
        self.settings.beat_aware_enabled = self.beat_aware_enabled
        self.settings.beat_threshold = self.beat_threshold

    def cleanup(self):
        self._update_settings_from_state()
        self.save_settings()
        self.stop_beat_detection()
        if hasattr(self, 'crossfade_timer'):
            GLib.source_remove(self.crossfade_timer)
            self.crossfade_timer = None
        if hasattr(self, 'player') and self.player:
            GLib.idle_add(self.player.set_state, Gst.State.NULL)
            bus = self.player.get_bus()
            if bus:
                bus.remove_signal_watch()
        if hasattr(self, 'crossfade_player') and self.crossfade_player:
            GLib.idle_add(self.crossfade_player.set_state, Gst.State.NULL)
        if hasattr(self, 'equalizer') and self.equalizer:
            GLib.idle_add(self.equalizer.set_state, Gst.State.NULL)

    def _resume_last_played(self):
        if not self.settings.last_played_track:
            return
        track_index = None
        for i, item in enumerate(self.playlist):
            if item.path == self.settings.last_played_track:
                track_index = i
                break
        if track_index is not None:
            if self.play_track(track_index):
                if self.settings.last_played_position > 2.0:
                    GLib.timeout_add(1000, self._seek_to_position, self.settings.last_played_position)
        else:
            pass

    def _seek_to_position(self, position):
        try:
            if hasattr(self, 'player') and self.player and self.playing:
                seek_pos = int(position * Gst.SECOND)
                self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_pos)
                pass
        except Exception:
            pass
        return False

    def _update_settings_from_state(self):
        if not hasattr(self, 'settings'):
            return
        self.settings.auto_play_next = self.auto_play_next
        self.settings.shuffle_mode = self.shuffle_mode
        self.settings.repeat_mode = self.repeat_mode
        self.settings.crossfade_enabled = self.crossfade_enabled
        self.settings.crossfade_duration = self.crossfade_duration
        self.settings.beat_aware_enabled = self.beat_aware_enabled
        self.settings.beat_threshold = self.beat_threshold
        if self.current_track >= 0 and self.current_track < len(self.playlist):
            current_item = self.playlist[self.current_track]
            self.settings.last_played_track = current_item.path
            if self.playing and hasattr(self, 'player') and self.player:
                try:
                    success, position = self.player.query_position(Gst.Format.TIME)
                    if success:
                        self.settings.last_played_position = position / Gst.SECOND
                except:
                    self.settings.last_played_position = 0.0
        if hasattr(self, 'player') and self.player:
            try:
                volume = self.player.get_property("volume")
                if volume is not None:
                    self.settings.volume = volume
            except Exception:
                pass
        if hasattr(self, 'get_default_size'):
            size = self.get_default_size()
            self.settings.window_size = (size.width, size.height)
        if hasattr(self, 'get_position'):
            self.settings.window_position = self.get_position()
        if hasattr(self, 'equalizer') and self.equalizer:
            self.settings.equalizer_settings = []
            for i in range(10):
                try:
                    value = self.equalizer.get_property('band' + str(i))
                    self.settings.equalizer_settings.append(value)
                except Exception:
                    pass
                    self.settings.equalizer_settings.append(0.0)

    def _apply_settings_to_state(self):
        if not hasattr(self, 'settings'):
            return
        self.auto_play_next = self.settings.auto_play_next
        self.shuffle_mode = self.settings.shuffle_mode
        self.repeat_mode = self.settings.repeat_mode
        self.crossfade_enabled = self.settings.crossfade_enabled
        self.crossfade_duration = self.settings.crossfade_duration
        self.beat_aware_enabled = self.settings.beat_aware_enabled
        self.beat_threshold = self.settings.beat_threshold
        if hasattr(self, 'player') and self.player:
            try:
                volume = max(0.0, min(1.0, self.settings.volume))
                self.player.set_property("volume", volume)
            except Exception:
                pass
        if hasattr(self, 'player_tab') and hasattr(self.player_tab, 'volume_scale'):
            try:
                volume_percent = self.settings.volume * 100
                clamped_volume = max(0, min(100, volume_percent))
                self.player_tab.volume_scale.set_value(clamped_volume)
                if hasattr(self.player_tab, 'volume_label'):
                    self.player_tab.volume_label.set_label(f"{int(clamped_volume)}%")
            except Exception:
                pass
        if hasattr(self, 'set_default_size'):
            width, height = self.settings.window_size
            self.set_default_size(width, height)
        if hasattr(self, 'move') and self.settings.window_position:
            x, y = self.settings.window_position
            self.move(x, y)
        if hasattr(self, 'equalizer') and self.equalizer and self.settings.equalizer_settings:
            for i, value in enumerate(self.settings.equalizer_settings[:10]):
                self.equalizer.set_property('band' + str(i), value)
        if hasattr(self, 'equalizer_tab') and self.settings.equalizer_settings:
            try:
                for i, value in enumerate(self.settings.equalizer_settings[:10]):
                    if i < len(self.equalizer_tab.band_scales):
                        scale, value_label = self.equalizer_tab.band_scales[i]
                        clamped_value = max(-12, min(12, value))
                        scale.set_value(clamped_value)
                        value_label.set_text(f"{clamped_value:+.1f} dB")
            except Exception:
                pass

    def _apply_ui_settings_delayed(self):
        if not hasattr(self, 'settings'):
            return False
        if hasattr(self, 'player_tab') and hasattr(self.player_tab, 'volume_scale'):
            try:
                volume_percent = self.settings.volume * 100
                clamped_volume = max(0, min(100, volume_percent))
                self.player_tab.volume_scale.set_value(clamped_volume)
                if hasattr(self.player_tab, 'volume_label'):
                    self.player_tab.volume_label.set_label(f"{int(clamped_volume)}%")
            except Exception:
                pass
        if hasattr(self, 'equalizer_tab') and self.settings.equalizer_settings:
            try:
                for i, value in enumerate(self.settings.equalizer_settings[:10]):
                    if i < len(self.equalizer_tab.band_scales):
                        scale, value_label = self.equalizer_tab.band_scales[i]
                        clamped_value = max(-12, min(12, value))
                        scale.set_value(clamped_value)
                        value_label.set_text(f"{value:+.1f} dB")
            except Exception:
                pass
        return

    def _apply_equalizer_settings_delayed(self):
        if hasattr(self, 'equalizer') and self.equalizer and hasattr(self, 'settings') and self.settings.equalizer_settings:
            for i, value in enumerate(self.settings.equalizer_settings[:10]):
                try:
                    self.equalizer.set_property('band' + str(i), value)
                    pass
                except Exception:
                    pass
        return False

    def save_settings_on_track_change(self):
        self._update_settings_from_state()
        self.save_settings()

    def save_settings_on_stop(self):
        self._update_settings_from_state()
        self.save_settings()

    def periodic_auto_save(self):
        if self.playing:
            self.auto_save_settings()
        return True

    def apply_xmms_css(self):
        css = """
        @define-color primary-bg #f0f0f0;
        @define-color secondary-bg #e0e0e0;
        @define-color accent-bg #d0d0d0;
        @define-color surface-bg #e8e8e8;
        @define-color highlight-bg #c0c0c0;
        @define-color primary-text #000000;
        @define-color secondary-text #404040;
        @define-color accent-text #0066cc;
        @define-color muted-text #808080;
        @define-color accent-color #0066cc;
        @define-color success-color #00aa44;
        @define-color warning-color #ff8800;
        @define-color danger-color #cc0000;
        @define-color border-color #cccccc;
        @define-color shadow-color rgba(0, 0, 0, 0.1);
        window {
            background: linear-gradient(135deg, @primary-bg 0%, #e8e8e8 100%);
            color: @primary-text;
            font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
            font-size: 12px;
            font-weight: 400;
        }
        .title-bar {
            background: linear-gradient(180deg, @secondary-bg 0%, @accent-bg 100%);
            color: @primary-text;
            padding: 4px 8px;
            border-bottom: 1px solid @border-color;
            box-shadow: 0 2px 8px @shadow-color;
            font-weight: 500;
        }
        .window-button {
            background: linear-gradient(135deg, @danger-color 0%, #cc0000 100%);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: white;
            border-radius: 50%;
            min-width: 18px;
            min-height: 18px;
            padding: 0;
            margin: 0;
            font-weight: bold;
            transition: all 0.2s ease;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }
        .window-button:hover {
            background: linear-gradient(135deg, #ff6666 0%, #ff0000 100%);
            transform: scale(1.1);
            box-shadow: 0 4px 8px rgba(255, 68, 68, 0.4);
        }
        .main-area, .eq-window, .playlist-window, .eq-tab, .playlist-tab, .player-tab {
            background: linear-gradient(145deg, @primary-bg 0%, @surface-bg 100%);
            border-radius: 8px;
            margin: 8px;
            padding: 12px;
            box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.3);
        }
        tooltip {
            background-color: white;
            color: black;
            border: 1px solid #cccccc;
            border-radius: 4px;
            padding: 4px 8px;
            font-size: 11px;
            font-weight: normal;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
            margin: 2px;
        }
        tooltip * {
            background-color: transparent;
            color: black;
        }
        button {
            background: linear-gradient(135deg, @accent-bg 0%, @highlight-bg 100%);
            border: 1px solid @border-color;
            color: #000000;
            border-radius: 6px;
            min-width: 32px;
            min-height: 28px;
            padding: 6px 12px;
            font-size: 11px;
            font-weight: 500;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 2px 4px @shadow-color, inset 0 1px 0 rgba(255, 255, 255, 0.1);
        }
        button:hover {
            background: linear-gradient(135deg, @highlight-bg 0%, #5a5a5a 100%);
            border-color: @accent-color;
            transform: translateY(-1px);
            box-shadow: 0 4px 8px @shadow-color, 0 0 12px rgba(0, 212, 255, 0.2);
        }
        button:active {
            transform: translateY(0);
            box-shadow: 0 1px 2px @shadow-color;
        }
        button.active {
            background: linear-gradient(135deg, @accent-color 0%, #0099cc 100%);
            color: white;
            border-color: @accent-text;
            box-shadow: 0 4px 12px rgba(0, 212, 255, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.3);
        }
        .control-button {
            background: linear-gradient(135deg, @secondary-bg 0%, @accent-bg 100%);
            border: 1px solid @border-color;
            border-radius: 8px;
            min-width: 40px;
            min-height: 30px;
            color: #000000;
            font-weight: 600;
            font-size: 13px;
            letter-spacing: 0.5px;
        }
        .control-button:hover {
            background: linear-gradient(135deg, @accent-color 0%, #0099cc 100%);
            border-color: @accent-text;
            box-shadow: 0 6px 16px rgba(0, 212, 255, 0.3);
        }
        .control-button.active {
            background: linear-gradient(135deg, @success-color 0%, #00cc66 100%);
            border-color: @success-color;
        }
        progressbar {
            background: transparent;
            border-radius: 12px;
            padding: 0;
            min-height: 12px;
            min-width: 200px;
            margin: 0 20px;
        }
        progressbar:focus-visible {
            outline: 2px solid @accent_bg_color;
            outline-offset: 2px;
        }
        progressbar * {
            min-width: 1px;
            min-height: 1px;
        }
        progressbar trough {
            background: alpha(@bg-color, 0.3);
            border: 1px solid alpha(@accent-bg, 0.3);
            border-radius: 6px;
            min-height: 10px;
            box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.1);
        }
        progressbar progress {
            background: linear-gradient(90deg, @accent-color 0%, #00c2ff 100%);
            border-radius: 5px;
            min-height: 8px;
            border: 1px solid alpha(white, 0.2);
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.2),
                0 1px 2px rgba(0, 0, 0, 0.1);
            transition: all 200ms ease-out;
        }
        progressbar:hover progress {
            background: linear-gradient(90deg, #5a9ff5 0%, #00d2ff 100%);
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.3),
                0 1px 3px rgba(0, 0, 0, 0.15);
        }
        progressbar:active progress {
            background: linear-gradient(90deg, #3a80d6 0%, #00a2d0 100%);
            box-shadow:
                inset 0 2px 3px rgba(0, 0, 0, 0.2),
                inset 0 1px 1px rgba(0, 0, 0, 0.1);
        }
        scale {
            min-width: 40px;
            min-height: 140px;
            margin: 4px 0;
            background: transparent;
        }
        scale trough {
            background: linear-gradient(90deg, @accent-bg 0%, @highlight-bg 100%);
            border-radius: 4px;
            min-height: 6px;
            box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.3);
        }
        scale trough highlight {
            background: linear-gradient(90deg, @accent-color 0%, #00ffaa 100%);
            border-radius: 4px;
            min-height: 6px;
        }
        scale slider {
            min-width: 24px;
            min-height: 24px;
            margin: 0 -6px;
            background: linear-gradient(135deg, @accent-color 0%, #0099cc 100%);
            border: 2px solid white;
            border-radius: 50%;
            box-shadow: 0 2px 6px @shadow-color;
            transition: all 0.2s ease;
        }
        scale slider:hover {
            transform: scale(1.2);
            box-shadow: 0 0 12px rgba(0, 212, 255, 0.6);
        }
        entry {
            background: @surface-bg;
            border: 1px solid @border-color;
            border-radius: 6px;
            color: @primary-text;
            padding: 8px 12px;
            font-size: 12px;
            transition: all 0.3s ease;
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.2);
        }
        entry:focus {
            border-color: @accent-color;
            box-shadow: 0 0 8px rgba(0, 212, 255, 0.3), inset 0 2px 4px rgba(0, 0, 0, 0.2);
        }
        label {
            color: @primary-text;
            font-weight: 400;
        }
        .track-label {
            font-size: 14px;
            font-weight: 500;
            color: @primary-text;
            margin-bottom: 4px;
        }
        .time-label {
            font-size: 11px;
            color: @secondary-text;
            font-family: 'Monaco', 'Consolas', monospace;
        }
        .stats-label {
            font-size: 11px;
            color: @muted-text;
            font-weight: 500;
        }
        .statusbar {
            background: linear-gradient(180deg, @accent-bg 0%, @secondary-bg 100%);
            border-top: 1px solid @border-color;
            color: @secondary-text;
            font-size: 11px;
            font-weight: 500;
            padding: 4px 12px;
            box-shadow: 0 -2px 8px @shadow-color;
        }
        notebook {
            background: transparent;
            border-radius: 8px;
            margin: 4px;
        }
        notebook header {
            background: linear-gradient(180deg, @secondary-bg 0%, @accent-bg 100%);
            border-radius: 8px 8px 0 0;
            border-bottom: 1px solid @border-color;
        }
        notebook tab {
            background: transparent;
            border: none;
            border-radius: 6px 6px 0 0;
            color: @secondary-text;
            padding: 8px 16px;
            font-weight: 500;
            transition: all 0.3s ease;
        }
        notebook tab:hover {
            background: rgba(0, 212, 255, 0.1);
            color: @primary-text;
        }
        notebook tab:checked {
            background: @primary-bg;
            color: @accent-color;
            box-shadow: inset 0 -2px 0 @accent-color;
        }
        scrolledwindow {
            background: @surface-bg;
            border: 1px solid @border-color;
            border-radius: 6px;
        }
        columnview {
            background: transparent;
        }
        columnview header {
            background: linear-gradient(180deg, @accent-bg 0%, @secondary-bg 100%);
            border-bottom: 1px solid @border-color;
        }
        columnview row {
            background: transparent;
            border-bottom: 1px solid rgba(64, 64, 64, 0.3);
            transition: all 0.2s ease;
        }
        columnview row:hover {
            background: rgba(0, 212, 255, 0.1);
        }
        columnview row:selected {
            background: linear-gradient(90deg, rgba(0, 212, 255, 0.2) 0%, rgba(0, 212, 255, 0.1) 100%);
            color: @accent-color;
        }
        dropdown {
            background: @accent-bg;
            border: 1px solid @border-color;
            border-radius: 6px;
            padding: 4px 8px;
            transition: all 0.3s ease;
        }
        dropdown:hover {
            border-color: @accent-color;
            box-shadow: 0 0 8px rgba(0, 212, 255, 0.2);
        }
        .eq-band {
            background: @surface-bg;
            border: 1px solid @border-color;
            border-radius: 6px;
            padding: 8px;
            margin: 2px;
        }
        .eq-label {
            color: @secondary-text;
            font-size: 10px;
            font-weight: 600;
        }
        .eq-scale {
            background: @accent-bg;
            border-radius: 4px;
            min-width: 40px;
            min-height: 140px;
        }
        .eq-scale trough {
            min-width: 4px;
            min-height: 120px;
        }
        .eq-scale slider {
            min-width: 12px;
            min-height: 12px;
        }
        .eq-preset-btn {
            background: linear-gradient(135deg, @secondary-bg 0%, @accent-bg 100%);
            border: 1px solid @border-color;
            border-radius: 4px;
            font-size: 10px;
            padding: 4px 8px;
            transition: all 0.2s ease;
        }
        .eq-preset-btn:hover {
            background: linear-gradient(135deg, @accent-color 0%, #0099cc 100%);
            border-color: @accent-text;
        }
        .section-header {
            font-size: 16px;
            font-weight: 600;
            color: @accent-color;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .search-entry {
            background: @surface-bg;
            border: 2px solid @border-color;
            border-radius: 8px;
            padding: 10px 14px;
            font-size: 13px;
            transition: all 0.3s ease;
        }
        .search-entry:focus {
            border-color: @accent-color;
            box-shadow: 0 0 12px rgba(0, 212, 255, 0.3);
        }
        .volume-icon {
            font-size: 16px;
            margin-right: 4px;
        }
        .volume-label {
            font-size: 11px;
            color: @secondary-text;
            font-weight: 500;
            min-width: 40px;
        }
        .eq-value-label {
            font-size: 10px;
            color: @accent-color;
            font-weight: 600;
            font-family: 'Monaco', 'Consolas', monospace;
        }
        .reset-button {
            background: linear-gradient(135deg, @warning-color 0%, #ff8800 100%);
            border: 1px solid @warning-color;
            color: white;
            font-weight: 600;
        }
        .reset-button:hover {
            background: linear-gradient(135deg, #ffcc00 0%, @warning-color 100%);
            box-shadow: 0 4px 12px rgba(255, 170, 0, 0.4);
        }
        .sort-dropdown {
            background: @surface-bg;
            border: 1px solid @border-color;
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 12px;
        }
        .sort-dropdown:hover {
            border-color: @accent-color;
        }
        .playlist-scrolled {
            background: @surface-bg;
            border: 1px solid @border-color;
            border-radius: 8px;
            box-shadow: inset 0 2px 6px rgba(0, 0, 0, 0.2);
        }
        .playlist-view {
            background: transparent;
        }
        .playlist-view header {
            background: linear-gradient(180deg, @accent-bg 0%, @secondary-bg 100%);
            border-bottom: 2px solid @border-color;
        }
        .playlist-view row {
            background: transparent;
            border-bottom: 1px solid rgba(64, 64, 64, 0.2);
            padding: 8px 12px;
            transition: all 0.2s ease;
        }
        .playlist-view row:hover {
            background: linear-gradient(90deg, rgba(0, 212, 255, 0.1) 0%, rgba(0, 212, 255, 0.05) 100%);
            border-left: 3px solid @accent-color;
            padding-left: 9px;
        }
        .playlist-view row:selected {
            background: linear-gradient(90deg, rgba(0, 212, 255, 0.2) 0%, rgba(0, 212, 255, 0.1) 100%);
            border-left: 3px solid @accent-color;
            color: @accent-color;
        }
        .playing-indicator {
            animation: pulse 2s infinite;
        }
        .playing-track {
            background: linear-gradient(90deg, rgba(0, 255, 136, 0.1) 0%, rgba(0, 255, 136, 0.05) 100%);
            border-left: 3px solid @success-color;
        }
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes glow {
            0% { box-shadow: 0 0 5px rgba(0, 212, 255, 0.5); }
            50% { box-shadow: 0 0 20px rgba(0, 212, 255, 0.8); }
            100% { box-shadow: 0 0 5px rgba(0, 212, 255, 0.5); }
        }
        .fade-in {
            animation: fadeIn 0.4s ease;
        }
        .slide-in {
            animation: slideIn 0.5s ease;
        }
        .glow-effect {
            animation: glow 2s infinite;
        }
        button:hover {
            background: linear-gradient(135deg, @highlight-bg 0%, #5a5a5a 100%);
            border-color: @accent-color;
            transform: translateY(-2px);
            box-shadow: 0 6px 12px @shadow-color, 0 0 16px rgba(0, 212, 255, 0.3);
        }
        button.active:hover {
            background: linear-gradient(135deg, @success-color 0%, #00cc66 100%);
            box-shadow: 0 8px 16px rgba(0, 255, 136, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.3);
        }
        button:focus {
            outline: 2px solid @accent-color;
            outline-offset: 2px;
        }
        entry:focus {
            outline: 2px solid @accent-color;
            outline-offset: 2px;
        }
        scrollbar {
            background: @accent-bg;
            border-radius: 4px;
        }
        scrollbar slider {
            background: @highlight-bg;
            border-radius: 4px;
            min-width: 8px;
            min-height: 8px;
            transition: all 0.2s ease;
        }
        scrollbar slider:hover {
            background: @accent-color;
        }
        progressbar.halfway progress {
            background: linear-gradient(90deg, @warning-color 0%, #ffcc00 100%);
            box-shadow: 0 0 12px rgba(255, 170, 0, 0.6);
        }
        progressbar.near-end progress {
            background: linear-gradient(90deg, @danger-color 0%, #ff6666 100%);
            box-shadow: 0 0 12px rgba(255, 68, 68, 0.6);
            animation: pulse 1s infinite;
        }
        .statusbar.playing {
            background: linear-gradient(180deg, rgba(0, 255, 136, 0.1) 0%, @accent-bg 100%);
            border-top-color: @success-color;
        }
        .playing-indicator {
            animation: pulse 2s infinite;
            color: @success-color;
        }
        .playing-track {
            background: linear-gradient(90deg, rgba(0, 255, 136, 0.1) 0%, rgba(0, 255, 136, 0.05) 100%);
            border-left: 3px solid @success-color;
        }
        .window-title {
            color: @primary-text;
            font-weight: 600;
            font-size: 14px;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
        }
        .main-container {
            background: linear-gradient(145deg, @primary-bg 0%, @surface-bg 100%);
        }
        .main-notebook {
            background: transparent;
            margin: 8px;
        }
        .main-notebook header {
            background: linear-gradient(180deg, @secondary-bg 0%, @accent-bg 100%);
            border-radius: 8px 8px 0 0;
            border-bottom: 2px solid @border-color;
        }
        .main-notebook tab {
            background: transparent;
            border: none;
            border-radius: 6px 6px 0 0;
            color: @secondary-text;
            padding: 10px 20px;
            font-weight: 500;
            transition: all 0.3s ease;
            margin: 4px 2px;
        }
        .main-notebook tab:hover {
            background: rgba(0, 212, 255, 0.1);
            color: @primary-text;
            transform: translateY(-1px);
        }
        .main-notebook tab:checked {
            background: @primary-bg;
            color: @accent-color;
            box-shadow: inset 0 -2px 0 @accent-color, 0 2px 4px rgba(0, 0, 0, 0.2);
        }
            .control-button {
                min-width: 32px;
                min-height: 28px;
                font-size: 11px;
            }
            .eq-band {
                min-width: 40px;
                padding: 4px;
            }
            .eq-scale {
                min-height: 120px;
            }
            .playlist-scrolled {
                min-height: 200px;
            }
            .section-header {
                font-size: 14px;
            }
        .compact-mode .main-toolbar {
        }
        .compact-mode .secondary_toolbar {
        }
        .compact-mode button {
            min-width: 28px;
            min-height: 24px;
            font-size: 10px;
        }
        .compact-mode .search-entry {
            padding: 6px 10px;
            font-size: 12px;
        }
        button:focus,
        entry:focus {
            outline: 2px solid @accent-color;
            outline-offset: 2px;
            border-radius: 4px;
        }
        @media (prefers-contrast: high) {
            button {
                border: 2px solid @primary-text;
            }
            button:focus {
                outline: 3px solid @accent-color;
            }
        }
        @media (prefers-color-scheme: light) {
            @define-color primary-bg #f0f0f0;
            @define-color secondary-bg #e0e0e0;
            @define-color accent-bg #d0d0d0;
            @define-color surface-bg #e8e8e8;
            @define-color highlight-bg #c0c0c0;
            @define-color primary-text #000000;
            @define-color secondary-text #404040;
            @define-color accent-text #0066cc;
            @define-color muted-text #808080;
            @define-color accent-color #0066cc;
            @define-color success-color #00aa00;
            @define-color warning-color #ff8800;
            @define-color danger-color #cc0000;
            @define-color border-color #a0a0a0;
            @define-color shadow-color rgba(0, 0, 0, 0.2);
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def setup_player(self):
        self.player = Gst.ElementFactory.make("playbin", "player")
        if not self.player:
            GLib.idle_add(self.set_status_message, "GStreamer initialization failed")
            return
        self.playing = False

        # Set up a simple audio sink first to avoid the bus error
        try:
            audio_sink = Gst.ElementFactory.make("autoaudiosink", "audio_sink")
            if not audio_sink:
                audio_sink = Gst.ElementFactory.make("pulsesink", "audio_sink")
            if not audio_sink:
                audio_sink = Gst.ElementFactory.make("alsasink", "audio_sink")

            if audio_sink:
                self.player.set_property("audio-sink", audio_sink)
        except Exception as e:
            print(f"Audio sink setup failed: {e}")

        # Try to set up equalizer separately if needed
        try:
            self.equalizer = Gst.ElementFactory.make("equalizer-10bands", "equalizer")
            if self.equalizer:
                # Create a custom audio sink bin with equalizer
                self.audio_sink = Gst.Bin.new("audio-sink")
                self.audio_convert = Gst.ElementFactory.make("audioconvert", "convert")
                self.audio_resample = Gst.ElementFactory.make("audioresample", "resample")
                output_sink = Gst.ElementFactory.make("autoaudiosink", "output")

                if all([self.audio_convert, self.audio_resample, output_sink]):
                    self.audio_sink.add(self.audio_convert)
                    self.audio_sink.add(self.audio_resample)
                    self.audio_sink.add(self.equalizer)
                    self.audio_sink.add(output_sink)

                    if (self.audio_convert.link(self.audio_resample) and
                        self.audio_resample.link(self.equalizer) and
                        self.equalizer.link(output_sink)):

                        sink_pad = Gst.GhostPad.new("sink", self.audio_convert.get_static_pad("sink"))
                        if sink_pad:
                            self.audio_sink.add_pad(sink_pad)
                            self.player.set_property("audio-sink", self.audio_sink)
        except Exception as e:
            print(f"Equalizer setup failed: {e}")
            self.equalizer = None

        try:
            self.player.set_property("volume", 0.7)
        except Exception:
            pass
        try:
            self.player.connect("about-to-finish", self.on_song_finished)
        except Exception:
            pass
        try:
            bus = self.player.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_bus_message)
        except Exception:
            pass

    def _setup_fallback_audio_sink(self):
        try:
            audio_sink = Gst.ElementFactory.make("autoaudiosink", "audio_sink")
            if audio_sink:
                self.player.set_property("audio-sink", audio_sink)
                return
            audio_sink = Gst.ElementFactory.make("pulsesink", "audio_sink")
            if audio_sink:
                self.player.set_property("audio-sink", audio_sink)
                return
            audio_sink = Gst.ElementFactory.make("alsasink", "audio_sink")
            if audio_sink:
                self.player.set_property("audio-sink", audio_sink)
                return
            else:
                pass
        except Exception:
            pass

    def setup_drag_drop(self):
        self.dnd = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        self.dnd.connect("drop", self.on_file_dropped)
        self.add_controller(self.dnd)

    def on_file_dropped(self, drop, file, x, y):
        path = file.get_path()
        if path:
            self.add_to_playlist([path])
            if not self.playing and self.playlist:
                self.play_track(0)
        return True

    def _set_player_state_thread_safe(self, state):
        try:
            return self.player.set_state(state)
        except Exception:
            return Gst.StateChangeReturn.FAILURE

    def _set_player_property_thread_safe(self, property_name, value):
        try:
            self.player.set_property(property_name, value)
            return True
        except Exception:
            return False

    def play_file(self, filepath, title=None):
        if not os.path.exists(filepath):
            pass
            return False
        try:
            uri = f"file://{os.path.abspath(filepath)}"
            if not hasattr(self, 'player') or not self.player:
                pass
                return False
            self.player.set_state(Gst.State.NULL)
            self.player.set_property("uri", uri)
            state_change = self.player.set_state(Gst.State.PLAYING)
            if state_change == Gst.StateChangeReturn.FAILURE:
                pass
                GLib.idle_add(self.set_status_message, "Failed to start playback")
                return False
            GLib.timeout_add(100, self._verify_playback_state, filepath, title)
            return True
        except Exception:
            return False

    def _verify_playback_state(self, filepath, title):
        try:
            if not hasattr(self, 'player') or not self.player:
                pass
                return False
            current_state = self.player.get_state(Gst.CLOCK_TIME_NONE)[1]
            if current_state == Gst.State.PLAYING:
                self.playing = True
                track_name = title or os.path.basename(filepath)
                GLib.idle_add(self.player_tab.track_label.set_text, track_name)
                GLib.idle_add(self.set_title, f"LinAmp - {track_name}")
            else:
                GLib.idle_add(self.set_status_message, "Playback failed - trying next track")
                if self.playlist and self.current_track < len(self.playlist) - 1:
                    GLib.idle_add(self.play_track, self.current_track + 1)
        except Exception:
            return False

    def on_play(self, button):
        if not self.playing:
            if self.playlist:
                if self.current_track < 0:
                    self.play_track(0)
                else:
                    GLib.idle_add(self.player.set_state, Gst.State.PLAYING)
                    self.playing = True
                    if self.beat_aware_enabled:
                        self.start_beat_detection()

    def on_pause(self, button):
        if self.playing:
            GLib.idle_add(self.player.set_state, Gst.State.PAUSED)
            self.playing = False
            self.stop_beat_detection()

    def on_stop(self, button):
        GLib.idle_add(self.player.set_state, Gst.State.NULL)
        self.playing = False
        self.stop_beat_detection()
        self.player_tab.time_label.set_text("0:00 / 0:00")
        self.player_tab.progress.set_fraction(0.0)
        self.player_tab.track_label.set_text("No track playing")
        self.set_title("LinAmp - XMMS Style")
        self.save_settings_on_stop()

    def on_volume_changed(self, scale):
        volume = scale.get_value() / 100.0
        try:
            if hasattr(self, 'player_tab') and hasattr(self.player_tab, 'volume_label'):
                self.player_tab.volume_label.set_label(f"{int(scale.get_value())}%")
            self.player.set_property("volume", volume)
        except Exception:
            pass
        self.auto_save_settings()

    def on_song_finished(self, element):
        if self.auto_play_next:
            next_index = self.get_next_track_index()
            if next_index is not None and self.crossfade_enabled:
                next_track = self.playlist[next_index]
                GLib.idle_add(self.start_crossfade, next_track.path)
            elif next_index is not None:
                GLib.idle_add(self.play_next_track)

    def on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            error_msg = str(err.message).lower()
            pass
            if "internal" in error_msg and "stream" in error_msg:
                pass
                GLib.idle_add(self.set_status_message, "Internal stream error - recovering...")
                try:
                    current_uri = self.player.get_property("uri")
                    GLib.idle_add(self.player.set_state, Gst.State.NULL)
                    GLib.timeout_add(100, lambda: GLib.idle_add(self._attempt_recovery, current_uri))
                except Exception as recovery_error:
                    pass
                    GLib.idle_add(self.set_status_message, "Recovery failed - trying next track")
                    if self.playlist and self.current_track < len(self.playlist) - 1:
                        GLib.idle_add(self.play_track, self.current_track + 1)
            else:
                GLib.idle_add(self.set_status_message, f"Error: {err.message}")
                if self.playlist and self.current_track < len(self.playlist) - 1:
                    GLib.idle_add(self.play_track, self.current_track + 1)
        elif message.type == Gst.MessageType.EOS:
            if self.auto_play_next:
                if self.repeat_mode == "one":
                    GLib.idle_add(self.play_track, self.current_track)
                elif self.repeat_mode == "all":
                    GLib.idle_add(self.play_next_track)
                else:
                    next_index = self.get_next_track_index()
                    if next_index is not None:
                        GLib.idle_add(self.play_track, next_index)
                    else:
                        GLib.idle_add(self.on_stop, None)
            else:
                GLib.idle_add(self.on_stop, None)
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src == self.player:
                old_state, new_state, pending_state = message.parse_state_changed()
                if new_state == Gst.State.PLAYING:
                    self.playing = True
                elif new_state in [Gst.State.PAUSED, Gst.State.NULL]:
                    self.playing = False
        return Gst.BusSyncReply.PASS

    def _attempt_recovery(self, uri):
        try:
            if not uri:
                pass
                return False
            self.player.set_property("uri", uri)
            GLib.idle_add(self.player.set_state, Gst.State.PLAYING)
            GLib.idle_add(self.set_status_message, "Recovery failed - trying next track")
            if self.playlist and self.current_track < len(self.playlist) - 1:
                GLib.idle_add(self.play_track, self.current_track + 1)
            return False
        except Exception:
            pass
            GLib.idle_add(self.set_status_message, "Recovery error - trying next track")
            if self.playlist and self.current_track < len(self.playlist) - 1:
                GLib.idle_add(self.play_track, self.current_track + 1)
            return False

    def update_display(self):
        try:
            if hasattr(self, 'player') and self.player and self.playing:
                success, position = self.player.query_position(Gst.Format.TIME)
                if not success:
                    position = 0
                success, duration = self.player.query_duration(Gst.Format.TIME)
                if not success:
                    duration = 0
                position_sec = position // Gst.SECOND
                duration_sec = duration // Gst.SECOND
                pos_str = f"{position_sec // 60}:{position_sec % 60:02d}"
                dur_str = f"{duration_sec // 60}:{duration_sec % 60:02d}"
                if hasattr(self, 'player_tab') and self.player_tab:
                    if hasattr(self.player_tab, 'time_label') and self.player_tab.time_label:
                        self.player_tab.time_label.set_text(f"{pos_str} / {dur_str}")
                        self.player_tab.time_label.add_css_class("time-label")
                    if hasattr(self.player_tab, 'progress') and self.player_tab.progress:
                        if duration_sec > 0 and position_sec >= 0:
                            fraction = position_sec / duration_sec
                            fraction = max(0.0, min(1.0, fraction))
                            current_fraction = self.player_tab.progress.get_fraction()
                            if abs(fraction - current_fraction) > 0.001:
                                self.player_tab.progress.set_fraction(fraction)
                                if fraction > 0.75:
                                    self.player_tab.progress.add_css_class("near-end")
                                    self.player_tab.progress.remove_css_class("halfway")
                                elif fraction > 0.5:
                                    self.player_tab.progress.remove_css_class("near-end")
                                    self.player_tab.progress.add_css_class("halfway")
                                else:
                                    self.player_tab.progress.remove_css_class("near-end")
                                    self.player_tab.progress.remove_css_class("halfway")
                        else:
                            self.player_tab.progress.set_fraction(0.0)
                    if hasattr(self.player_tab, 'track_label') and self.player_tab.track_label:
                        self.player_tab.track_label.add_css_class("playing-indicator")
            status_parts = []
            if self.repeat_mode != "none":
                status_parts.append(f"Repeat: {self.repeat_mode.upper()}")
            if self.shuffle_mode:
                status_parts.append("SHUFFLE")
            if not self.auto_play_next:
                status_parts.append("NO AUTO-NEXT")
            if self.crossfade_enabled:
                status_parts.append(f"XFADE: {self.crossfade_duration}s")
            if self.beat_aware_enabled:
                status_parts.append("BEAT-AWARE")
            status_text = " | ".join(status_parts) if status_parts else "Ready"
            if self.playing:
                status_text = f"🎵 {status_text}"
                if hasattr(self, 'status_bar'):
                    self.status_bar.add_css_class("playing")
            else:
                if hasattr(self, 'status_bar'):
                    self.status_bar.remove_css_class("playing")
            GLib.idle_add(self.set_status_message, status_text)
        except Exception:
            if "Gtk.Statusbar.remove() takes exactly 3 arguments (2 given)" in str(e):
                pass
                return True
            pass
            pass
            pass
        return True

    def play_track(self, index):
        if 0 <= index < len(self.playlist):
            item = self.playlist[index]
            if self.repeat_mode == "one" and index == self.current_track:
                GLib.idle_add(self.player.set_state, Gst.State.NULL)
            if self.play_file(item.path, item.title):
                self.current_track = index
                self.save_settings_on_track_change()
                if self.shuffle_mode and index in self.shuffled_indices:
                    self.shuffle_position = self.shuffled_indices.index(index)
                if hasattr(self, 'playlist_tab') and hasattr(self.playlist_tab, 'selection_model'):
                    try:
                        self.playlist_tab.selection_model.set_selected(index)
                    except:
                        pass
                return True
        return False

    def on_prev(self, button):
        self.play_previous_track()

    def play_previous_track(self):
        if not self.playlist:
            return
        prev_index = self.get_previous_track_index()
        if prev_index is not None:
            self.play_track(prev_index)

    def get_previous_track_index(self):
        if not self.playlist:
            return None
        if self.shuffle_mode:
            return self.get_previous_shuffled_index()
        else:
            if self.current_track > 0:
                return self.current_track - 1
            elif self.repeat_mode == "all":
                return len(self.playlist) - 1
            else:
                return None

    def get_previous_shuffled_index(self):
        if not self.shuffled_indices:
            self.regenerate_shuffle_list()
        if self.shuffle_position > 0:
            self.shuffle_position -= 1
            return self.shuffled_indices[self.shuffle_position]
        elif self.repeat_mode == "all":
            self.shuffle_position = len(self.shuffled_indices) - 1
            return self.shuffled_indices[self.shuffle_position]
        else:
            return None

    def toggle_repeat_mode(self):
        modes = ["none", "one", "all"]
        current_index = modes.index(self.repeat_mode)
        self.repeat_mode = modes[(current_index + 1) % len(modes)]
        self.update_status_display()
        self.auto_save_settings()

    def toggle_shuffle_mode(self):
        self.shuffle_mode = not self.shuffle_mode
        if self.shuffle_mode:
            self.regenerate_shuffle_list()
            if self.current_track >= 0:
                try:
                    self.shuffle_position = self.shuffled_indices.index(self.current_track)
                except ValueError:
                    self.shuffle_position = -1
        self.update_status_display()
        self.auto_save_settings()

    def toggle_auto_play_next(self):
        self.auto_play_next = not self.auto_play_next
        self.update_status_display()
        self.auto_save_settings()

    def toggle_crossfade(self):
        self.crossfade_enabled = not self.crossfade_enabled
        self.update_status_display()
        self.auto_save_settings()

    def toggle_beat_aware(self):
        self.beat_aware_enabled = not self.beat_aware_enabled
        self.update_status_display()
        self.auto_save_settings()

    def auto_save_settings(self):
        if hasattr(self, '_auto_save_timer') and self._auto_save_timer:
            try:
                GLib.source_remove(self._auto_save_timer)
                self._auto_save_timer = None
            except:
                pass
        self._auto_save_timer = GLib.timeout_add(2000, self._auto_save_callback)

    def _auto_save_callback(self):
        self._update_settings_from_state()
        self.save_settings()
        if self.beat_aware_enabled:
            self.start_beat_detection()
        else:
            self.stop_beat_detection()
        self._auto_save_timer = None
        return False

    def update_status_display(self):
        status_parts = []
        if self.repeat_mode != "none":
            status_parts.append(f"Repeat: {self.repeat_mode.upper()}")
        if self.shuffle_mode:
            status_parts.append("SHUFFLE")
        if not self.auto_play_next:
            status_parts.append("NO AUTO-NEXT")
        if self.crossfade_enabled:
            status_parts.append(f"XFADE: {self.crossfade_duration}s")
        if self.beat_aware_enabled:
            status_parts.append("BEAT-AWARE")
        status_text = " | ".join(status_parts) if status_parts else "Normal playback"
        GLib.idle_add(self.set_status_message, status_text)
        self.update_button_states()

    def update_button_states(self):
        if not hasattr(self, 'player_tab'):
            return
        if hasattr(self.player_tab, 'repeat_btn'):
            if self.repeat_mode == "none":
                self.player_tab.repeat_btn.remove_css_class("active")
                self.player_tab.repeat_btn.set_tooltip_text("Repeat: Off")
            elif self.repeat_mode == "one":
                self.player_tab.repeat_btn.add_css_class("active")
                self.player_tab.repeat_btn.set_tooltip_text("Repeat: One Track")
            else:
                self.player_tab.repeat_btn.add_css_class("active")
                self.player_tab.repeat_btn.set_tooltip_text("Repeat: All")
        if hasattr(self.player_tab, 'shuffle_btn'):
            if self.shuffle_mode:
                self.player_tab.shuffle_btn.add_css_class("active")
                self.player_tab.shuffle_btn.set_tooltip_text("Shuffle: On")
            else:
                self.player_tab.shuffle_btn.remove_css_class("active")
                self.player_tab.shuffle_btn.set_tooltip_text("Shuffle: Off")
        if hasattr(self.player_tab, 'crossfade_btn'):
            if self.crossfade_enabled:
                self.player_tab.crossfade_btn.add_css_class("active")
                self.player_tab.crossfade_btn.set_tooltip_text(f"Crossfade: {self.crossfade_duration}s")
            else:
                self.player_tab.crossfade_btn.remove_css_class("active")
                self.player_tab.crossfade_btn.set_tooltip_text("Crossfade: Off")
        if hasattr(self.player_tab, 'beat_btn'):
            if self.beat_aware_enabled:
                self.player_tab.beat_btn.add_css_class("active")
                self.player_tab.beat_btn.set_tooltip_text("Beat Detection: On")
            else:
                self.player_tab.beat_btn.remove_css_class("active")
                self.player_tab.beat_btn.set_tooltip_text("Beat Detection: Off")
        if hasattr(self.player_tab, 'autonext_btn'):
            if self.auto_play_next:
                self.player_tab.autonext_btn.add_css_class("active")
                self.player_tab.autonext_btn.set_tooltip_text("Auto Next: On")
            else:
                self.player_tab.autonext_btn.remove_css_class("active")
                self.player_tab.autonext_btn.set_tooltip_text("Auto Next: Off")

    def on_next(self, button):
        self.play_next_track()

    def play_next_track(self):
        if not self.playlist:
            return
        next_index = self.get_next_track_index()
        if next_index is not None:
            self.play_track(next_index)

    def get_next_track_index(self):
        if not self.playlist:
            return None
        if self.shuffle_mode:
            return self.get_next_shuffled_index()
        else:
            if self.current_track < len(self.playlist) - 1:
                return self.current_track + 1
            elif self.repeat_mode == "all":
                return 0
            else:
                return None

    def get_next_shuffled_index(self):
        if not self.shuffled_indices:
            self.regenerate_shuffle_list()
        if self.shuffle_position < len(self.shuffled_indices) - 1:
            self.shuffle_position += 1
            return self.shuffled_indices[self.shuffle_position]
        elif self.repeat_mode == "all":
            self.regenerate_shuffle_list()
            self.shuffle_position = 0
            return self.shuffled_indices[0]
        else:
            return None

    def regenerate_shuffle_list(self):
        self.original_indices = list(range(len(self.playlist)))
        self.shuffled_indices = self.original_indices.copy()
        random.shuffle(self.shuffled_indices)
        self.shuffle_position = -1

    def start_beat_detection(self):
        if not self.beat_aware_enabled or not self.playing:
            return
        self.beat_detection_timer = GLib.timeout_add(50, self.detect_beat)

    def stop_beat_detection(self):
        if self.beat_detection_timer:
            try:
                GLib.source_remove(self.beat_detection_timer)
            except:
                pass
            self.beat_detection_timer = None

    def detect_beat(self):
        if not self.beat_aware_enabled or not self.playing:
            return False
        try:
            success_pos, position = self.player.query_position(Gst.Format.TIME)
            success_dur, duration = self.player.query_duration(Gst.Format.TIME)
            if not success_pos or not success_dur:
                return True
            current_time = time.time()
            position_sec = position / Gst.SECOND
            if current_time - self.last_beat_time > 0.3:
                self.last_beat_time = current_time
                if hasattr(self, 'last_beat_position'):
                    interval = position_sec - self.last_beat_position
                    if 0.2 < interval < 2.0:
                        self.beat_interval_history.append(interval)
                        if len(self.beat_interval_history) > 10:
                            self.beat_interval_history.pop(0)
                        if len(self.beat_interval_history) >= 4:
                            avg_interval = sum(self.beat_interval_history) / len(self.beat_interval_history)
                            bpm = 60.0 / avg_interval
                            self.on_beat_detected(bpm)
                self.last_beat_position = position_sec
        except Exception:
            return True

    def on_beat_detected(self, bpm):
        pass

    def setup_crossfade_player(self):
        try:
            self.crossfade_player = Gst.ElementFactory.make("playbin", "crossfade_player")
            if self.crossfade_player:
                if hasattr(self, 'audio_sink'):
                    self.crossfade_player.set_property("audio-sink", self.audio_sink)
                else:
                    audio_sink = Gst.ElementFactory.make("autoaudiosink", "crossfade_sink")
                    if audio_sink:
                        self.crossfade_player.set_property("audio-sink", audio_sink)
                self.crossfade_player.set_property("volume", 0.0)
                try:
                    crossfade_bus = self.crossfade_player.get_bus()
                    if crossfade_bus:
                        crossfade_bus.add_signal_watch()
                except Exception:
                    pass
                return True
        except Exception:
            return False

    def start_crossfade(self, next_track_path):
        if not self.crossfade_enabled or not self.crossfade_player:
            if not self.setup_crossfade_player():
                GLib.idle_add(self.play_next_track)
                return
        try:
            uri = f"file://{os.path.abspath(next_track_path)}"
            self.crossfade_player.set_property("uri", uri)
            GLib.idle_add(self.crossfade_player.set_state, Gst.State.PLAYING)
            self.crossfade_start_time = time.time()
            self.crossfade_timer = GLib.timeout_add(50, self.update_crossfade)
        except Exception:
            pass
            GLib.idle_add(self.play_next_track)

    def update_crossfade(self):
        if not self.crossfade_enabled or not self.crossfade_player:
            return False
        try:
            elapsed = time.time() - self.crossfade_start_time
            progress = min(elapsed / self.crossfade_duration, 1.0)
            current_volume = 1.0 - progress
            next_volume = progress
            self.player.set_property("volume", current_volume)
            self.crossfade_player.set_property("volume", next_volume)
            if progress >= 1.0:
                self.complete_crossfade()
                return False
        except Exception:
            pass
            self.complete_crossfade()
            return False
        return True

    def complete_crossfade(self):
        try:
            self.player.set_state(Gst.State.NULL)
            self.player, self.crossfade_player = self.crossfade_player, self.player
            self.playing = True
            self.player.set_property("volume", self.player_tab.volume_scale.get_value() / 100.0)
            if hasattr(self, 'crossfade_timer'):
                GLib.source_remove(self.crossfade_timer)
                self.crossfade_timer = None
        except Exception:
            pass

    def on_eq_clicked(self, button):
        self.notebook.set_current_page(1)

    def on_pl_clicked(self, button):
        self.notebook.set_current_page(2)

    def add_to_playlist(self, file_paths):
        added_items = []
        for path in file_paths:
            if os.path.exists(path):
                item = PlaylistItem(path=path, title=os.path.basename(path))
                self.playlist.append(item)
                added_items.append(item)
        if hasattr(self, 'playlist_tab') and added_items:
            for item in added_items:
                self.playlist_tab.playlist_store.append(item.title)
            self.playlist_tab.update_statistics()
        self.save_playlist()

    def add_folder_to_playlist(self, folder_path):
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            pass
            return
        added_items = []
        audio_extensions = {'.mp3', '.mp4', '.flac', '.ogg', '.wav', '.m4a', '.wma', '.aac', '.opus'}
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                file_ext = os.path.splitext(file)[1].lower()
                if file_ext in audio_extensions:
                    try:
                        rel_path = os.path.relpath(file_path, folder_path)
                        title = rel_path if rel_path != file_path else file
                        item = PlaylistItem(path=file_path, title=title)
                        self.playlist.append(item)
                        added_items.append(item)
                    except Exception:
                        pass
        if added_items:
            if hasattr(self, 'playlist_tab'):
                for item in added_items:
                    self.playlist_tab.playlist_store.append(item.title)
                self.playlist_tab.update_statistics()
            self.save_playlist()
        else:
            pass

    def save_playlist(self, filepath: str = None) -> bool:
        if not hasattr(self, 'playlist') or not self.playlist:
            pass
            return False
        if not filepath:
            playlist_dir = os.path.expanduser("~/.config/linamp")
            filepath = os.path.join(playlist_dir, "playlist.json")
        else:
            filepath = os.path.abspath(os.path.expanduser(filepath))
            playlist_dir = os.path.dirname(filepath)
        try:
            os.makedirs(playlist_dir, exist_ok=True)
        except (OSError, PermissionError) as e:
            pass
            return False
        try:
            playlist_data = []
            for item in self.playlist:
                try:
                    if hasattr(item, 'to_dict') and callable(item.to_dict):
                        if hasattr(item, 'path') and item.path and os.path.exists(item.path):
                            playlist_data.append(item.to_dict())
                        else:
                            pass
                except Exception:
                    pass
            if not playlist_data:
                pass
                return False
        except Exception:
            pass
            return False
        temp_file = None
        try:
            fd, temp_file = tempfile.mkstemp(
                prefix='.playlist_',
                suffix='.tmp',
                dir=playlist_dir,
                text=True
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(playlist_data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(filepath):
                os.replace(temp_file, filepath)
            else:
                os.rename(temp_file, filepath)
            return True
        except (OSError, IOError) as e:
            pass
        except json.JSONEncodeError as e:
            pass
        except Exception:
            pass
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass
        return False

    def _clear_playlist_store(self) -> bool:
        if not hasattr(self, 'playlist_tab') or not hasattr(self.playlist_tab, 'playlist_store'):
            pass
            return False
        try:
            store = self.playlist_tab.playlist_store
            store.freeze_notify()
            if hasattr(store, 'splice') and callable(store.splice):
                store.splice(0, store.get_n_items(), [])
            else:
                while store.get_n_items() > 0:
                    store.remove(store.get_n_items() - 1)
            return True
        except Exception:
            pass
            return False
        finally:
            if 'store' in locals():
                try:
                    store.thaw_notify()
                except Exception:
                    pass

    def _update_playlist_display(self) -> bool:
        if not hasattr(self, 'playlist_tab') or not hasattr(self.playlist_tab, 'playlist_store'):
            pass
            return False
        try:
            store = self.playlist_tab.playlist_store
            if not self.playlist:
                pass
                return True
            store.freeze_notify()
            display_names = []
            for item in self.playlist:
                try:
                    if isinstance(item, PlaylistItem):
                        display_names.append(item.get_display_name())
                    elif isinstance(item, dict):
                        display_names.append(item.get('title') or os.path.basename(item.get('path', '')))
                    else:
                        display_names.append(str(item))
                except Exception:
                    pass
                    display_names.append("<Invalid Item>")
            if hasattr(store, 'splice') and callable(store.splice):
                store.splice(0, store.get_n_items(), display_names)
            else:
                for name in display_names:
                    store.append(name)
            pass
            return True
        except Exception:
            pass
            return False
        finally:
            if 'store' in locals():
                try:
                    store.thaw_notify()
                except Exception:
                    pass
            if hasattr(self, 'playlist_tab') and hasattr(self.playlist_tab, 'update_statistics'):
                self.playlist_tab.update_statistics()

    def load_playlist(self, filepath: str = None) -> bool:
        if not filepath:
            playlist_dir = os.path.expanduser("~/.config/linamp")
            filepath = os.path.join(playlist_dir, "playlist.json")
        else:
            filepath = os.path.abspath(os.path.expanduser(filepath))
        if not os.path.exists(filepath):
            return False
        if not os.access(filepath, os.R_OK):
            pass
            return False
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        pass
                        return False
                except json.JSONDecodeError:
                    pass
                    return False
        except (IOError, OSError):
            pass
            return False
        except Exception:
            pass
            return False
        playlist = []
        invalid_items = 0
        for i, item_data in enumerate(data, 1):
            try:
                if not isinstance(item_data, dict):
                    pass
                    invalid_items += 1
                    continue
                try:
                    item = PlaylistItem.from_dict(item_data)
                    if not item.exists():
                        pass
                    playlist.append(item)
                except ValueError:
                    pass
                    invalid_items += 1
            except Exception:
                pass
                invalid_items += 1
        self.playlist = playlist
        total_items = len(data)
        loaded_items = len(playlist)
        if hasattr(self, 'playlist_tab') and self.playlist_tab:
            self._clear_playlist_store()
            self._update_playlist_display()
        return len(playlist) > 0

class LinAmpApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='org.example.linamp.xmms')
        self.win = None

    def do_activate(self):
        try:
            if not self.win:
                self.win = WinampWindow(application=self, title="LinAmp")
                self.win.present()
            else:
                self.win.present()
        except Exception:
            pass

    def do_shutdown(self):
        if self.win:
            self.win.cleanup()
        Gtk.Application.do_shutdown(self)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.setup_menu()
        if len(sys.argv) > 1:
            if not self.win:
                self.win = WinampWindow(application=self, title="LinAmp")
            self.win.add_to_playlist(sys.argv[1:])
            if self.win.playlist:
                self.win.play_track(0)

    def setup_menu(self):
        open_action = Gio.SimpleAction.new("open", None)
        open_action.connect("activate", self.on_open)
        open_folder_action = Gio.SimpleAction.new("open_folder", None)
        open_folder_action.connect("activate", self.on_open_folder)
        add_to_playlist_action = Gio.SimpleAction.new("add_to_playlist", None)
        add_to_playlist_action.connect("activate", self.on_add_to_playlist)
        add_folder_to_playlist_action = Gio.SimpleAction.new("add_folder_to_playlist", None)
        add_folder_to_playlist_action.connect("activate", self.on_add_folder_to_playlist)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(open_action)
        self.add_action(open_folder_action)
        self.add_action(add_to_playlist_action)
        self.add_action(add_folder_to_playlist_action)
        self.add_action(quit_action)
        menu = Gio.Menu()
        file_menu = Gio.Menu()
        file_menu.append("Open File", "app.open")
        file_menu.append("Open Folder", "app.open_folder")
        file_menu.append("Add to Playlist", "app.add_to_playlist")
        file_menu.append("Add Folder to Playlist", "app.add_folder_to_playlist")
        file_menu.append("Quit", "app.quit")
        menu.append_submenu("File", file_menu)
        self.set_menubar(menu)

    def on_open(self, action, param):
        dialog = Gtk.FileChooserNative.new(
            title="Open Audio File",
            parent=self.win,
            action=Gtk.FileChooserAction.OPEN
        )
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        audio_filter.add_mime_type("audio/*")
        dialog.add_filter(audio_filter)
        dialog.connect("response", self.on_file_chooser_response)
        dialog.set_visible(True)

    def on_add_to_playlist(self, action, param):
        dialog = Gtk.FileChooserNative.new(
            title="Add to Playlist",
            parent=self.win,
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=True
        )
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        audio_filter.add_mime_type("audio/*")
        dialog.add_filter(audio_filter)
        dialog.connect("response", self.on_add_to_playlist_response)
        dialog.set_visible(True)

    def on_open_folder(self, action, param):
        dialog = Gtk.FileChooserNative.new(
            title="Open Music Folder",
            parent=self.win,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.connect("response", self.on_folder_chooser_response)
        dialog.set_visible(True)

    def on_add_folder_to_playlist(self, action, param):
        dialog = Gtk.FileChooserNative.new(
            title="Add Music Folder to Playlist",
            parent=self.win,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.connect("response", self.on_add_folder_to_playlist_response)
        dialog.set_visible(True)

    def on_file_chooser_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                self.win.playlist.clear()
                if hasattr(self.win, 'playlist_tab'):
                    store = self.win.playlist_tab.playlist_store
                    while store.get_n_items() > 0:
                        store.remove(0)
                self.win.add_to_playlist([file.get_path()])
                self.win.play_track(0)
        dialog.destroy()

    def on_add_to_playlist_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            files = dialog.get_files()
            file_paths = [f.get_path() for f in files if f.get_path()]
            if file_paths:
                was_empty = not self.win.playlist
                self.win.add_to_playlist(file_paths)
                if was_empty and self.win.playlist:
                    self.win.play_track(0)
        dialog.destroy()

    def on_folder_chooser_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file()
            if folder:
                folder_path = folder.get_path()
                self.win.playlist.clear()
                if hasattr(self.win, 'playlist_tab'):
                    store = self.win.playlist_tab.playlist_store
                    while store.get_n_items() > 0:
                        store.remove(0)
                self.win.add_folder_to_playlist(folder_path)
                if self.win.playlist:
                    self.win.play_track(0)
        dialog.destroy()

    def on_add_folder_to_playlist_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file()
            if folder:
                folder_path = folder.get_path()
                was_empty = not self.win.playlist
                self.win.add_folder_to_playlist(folder_path)
                if was_empty and self.win.playlist:
                    self.win.play_track(0)
        dialog.destroy()

def main():
    app = LinAmpApp()
    app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())