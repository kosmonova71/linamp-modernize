#!/usr/bin/env python3
# LinAmp Classic — GTK4 Winamp Clone (Pixel-Perfect)
import gi, sys, os, time
from pathlib import Path

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Gst', '1.0')

from gi.repository import Gtk, Gdk, GLib, Gst, GdkPixbuf, Gdk

# Initialize GStreamer
Gst.init(None)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MAIN_BMP = DATA_DIR / "main.bmp"       # Winamp main background
BUTTONS_BMP = DATA_DIR / "cbuttons.bmp" # Buttons bitmap
WINDOW_WIDTH, WINDOW_HEIGHT = 275, 116

# Hitboxes (x, y, width, height)
BUTTONS = {
    "play": (16, 88, 23, 18),
    "pause": (39, 88, 23, 18),
    "stop": (62, 88, 23, 18),
    "prev": (0, 88, 16, 18),
    "next": (85, 88, 23, 18)
}
SEEK_BAR = (114, 96, 145, 6)
VOLUME_BAR = (233, 88, 40, 18)

class WinampClone(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.linamp.classic")
        self.player = Gst.ElementFactory.make("playbin")
        self.current_uri = None
        self.volume = 0.8
        self.player.set_property("volume", self.volume)
        self.track_title = "No track loaded"
        self.scroll_offset = 0

        self.load_assets()
        # GStreamer bus
        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

    def load_assets(self):
        self.bg_pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(MAIN_BMP))
        self.buttons_pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(BUTTONS_BMP))

    def do_activate(self):
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title("LinAmp Classic")
        self.win.set_resizable(False)
        self.win.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_content_width(WINDOW_WIDTH)
        self.canvas.set_content_height(WINDOW_HEIGHT)
        self.canvas.set_draw_func(self.draw_ui)

        self.canvas.add_controller(Gtk.GestureClick.new())
        self.canvas.add_controller(Gtk.GestureDrag.new())
        self.win.set_child(self.canvas)

        self.canvas.connect("button-press-event", self.on_click)
        self.canvas.set_focusable(True)

        self.win.present()
        GLib.timeout_add(50, self.update_ui)

    # ---------------- UI ----------------
    def draw_ui(self, area, cr, width, height):
        # Draw background
        Gdk.cairo_set_source_pixbuf(cr, self.bg_pixbuf, 0, 0)
        cr.paint()
        # Draw buttons
        Gdk.cairo_set_source_pixbuf(cr, self.buttons_pixbuf, 0, 0)
        cr.paint()
        # Draw track title (scrolling)
        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face("Verdana", 0, 0)
        cr.set_font_size(10)
        title_x = 12 - self.scroll_offset
        cr.move_to(title_x, 14)
        cr.show_text(self.track_title)
        # Seekbar fill
        if self.current_uri:
            ok1, pos = self.player.query_position(Gst.Format.TIME)
            ok2, dur = self.player.query_duration(Gst.Format.TIME)
            if ok1 and ok2 and dur > 0:
                pct = pos / dur
                x, y, w, h = SEEK_BAR
                cr.set_source_rgb(0, 1, 0)
                cr.rectangle(x, y, int(w * pct), h)
                cr.fill()
        # Volume bar fill
        x, y, w, h = VOLUME_BAR
        cr.set_source_rgb(0, 1, 0)
        cr.rectangle(x, y, int(w * self.volume), h)
        cr.fill()

    # ---------------- Playback ----------------
    def play_uri(self, uri):
        self.current_uri = uri
        self.track_title = Path(uri.replace("file://", "")).name
        self.scroll_offset = 0
        self.player.set_state(Gst.State.NULL)
        self.player.set_property("uri", uri)
        self.player.set_state(Gst.State.PLAYING)

    def toggle_pause(self):
        state = self.player.get_state(0).state
        if state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.PAUSED)
        elif state == Gst.State.PAUSED:
            self.player.set_state(Gst.State.PLAYING)

    def stop(self):
        self.player.set_state(Gst.State.NULL)

    # ---------------- Click Handling ----------------
    def on_click(self, widget, event):
        x, y = event.x, event.y
        for name, (bx, by, bw, bh) in BUTTONS.items():
            if bx <= x <= bx + bw and by <= y <= by + bh:
                if name == "play" and self.current_uri:
                    self.player.set_state(Gst.State.PLAYING)
                elif name == "pause":
                    self.toggle_pause()
                elif name == "stop":
                    self.stop()
                elif name == "prev":
                    pass # extend for playlist
                elif name == "next":
                    pass # extend for playlist
                return True
        # Seekbar
        sx, sy, sw, sh = SEEK_BAR
        if sy <= y <= sy + sh and sx <= x <= sx + sw:
            ok1, dur = self.player.query_duration(Gst.Format.TIME)
            if ok1 and dur > 0:
                t = (x - sx) / sw * dur
                self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(t))
            return True
        # Volume bar
        vx, vy, vw, vh = VOLUME_BAR
        if vy <= y <= vy + vh and vx <= x <= vx + vw:
            self.volume = (x - vx) / vw
            self.player.set_property("volume", self.volume)
            return True
        # Open file click area (top left)
        if 0 <= x <= 16 and 0 <= y <= 16:
            self.open_file()
            return True

    def open_file(self):
        dialog = Gtk.FileDialog(title="Open Audio")
        dialog.open(self.win, None, self._file_opened)

    def _file_opened(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self.play_uri(file.get_uri())
        except Exception:
            pass

    # ---------------- UI Update ----------------
    def update_ui(self):
        if self.track_title:
            self.scroll_offset += 1
            if self.scroll_offset > len(self.track_title) * 7:  # approx char width
                self.scroll_offset = 0
        self.canvas.queue_draw()
        return True

    # ---------------- GStreamer Bus ----------------
    def on_bus_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"GStreamer Error: {err}, {debug}")
            self.stop()

if __name__ == "__main__":
    app = WinampClone()
    app.run(sys.argv)
