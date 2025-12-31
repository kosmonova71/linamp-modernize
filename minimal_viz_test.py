#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gst', '1.0')

from gi.repository import Gtk, GLib
import time

class TestWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Visualization Test")
        self.set_default_size(400, 300)
        
        # Create a simple drawing area
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_hexpand(True)
        drawing_area.set_vexpand(True)
        drawing_area.set_draw_func(self.on_draw, None)
        
        # Create a box
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.append(Gtk.Label(label="Test Visualization"))
        box.append(drawing_area)
        
        self.set_child(box)
        
        # Start a timer to trigger redraws
        GLib.timeout_add(100, self.update_visualizer)
    
    def on_draw(self, area, cr, width, height, user_data=None):
        print(f"Drawing: {width}x{height}")
        
        # Clear background
        cr.set_source_rgb(0.2, 0.1, 0.3)
        cr.paint()
        
        # Draw test rectangle
        cr.set_source_rgb(1.0, 1.0, 0.0)
        cr.rectangle(10, 10, 50, 50)
        cr.fill()
        
        # Draw animated bars
        time_factor = time.time() * 2.0
        for i in range(8):
            height_factor = 0.3 + 0.2 * time.sin(time_factor + i * 0.5)
            bar_height = height * height_factor * 0.8
            
            cr.set_source_rgb(0.3 + 0.7 * (i / 8), 0.5, 0.8)
            bar_width = width / 8
            x = i * bar_width
            y = height - bar_height
            cr.rectangle(x + 2, y, bar_width - 4, bar_height)
            cr.fill()
    
    def update_visualizer(self):
        if hasattr(self, 'drawing_area') and self.drawing_area:
            self.drawing_area.queue_draw()
        return True

class TestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.test.visualization")
    
    def do_activate(self):
        win = TestWindow(self)
        win.present()

if __name__ == "__main__":
    app = TestApp()
    app.run()
