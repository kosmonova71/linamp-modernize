#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
import time

class TestVisualization(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Test Visualization")
        self.set_default_size(400, 300)
        
        # Create a simple drawing area
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)
        self.drawing_area.set_size_request(-1, 200)
        
        print("Created drawing area")
        
        # Connect the draw function
        self.drawing_area.set_draw_func(self.on_draw, None)
        print("Connected draw function")
        
        # Create a box
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.append(Gtk.Label(label="Test Visualization"))
        box.append(self.drawing_area)
        
        self.set_child(box)
        
        # Start timer to trigger redraws
        print("Starting timer...")
        GLib.timeout_add(100, self.update_visualizer)
        
        # Force initial draw
        GLib.idle_add(lambda: self.drawing_area.queue_draw())
        print("Forced initial draw")
    
    def on_draw(self, area, cr, width, height, user_data=None):
        print(f"*** DRAW CALLED: {width}x{height} ***")
        
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
            height_factor = 0.3 + 0.2 * math.sin(time_factor + i * 0.5)
            bar_height = height * height_factor * 0.8
            
            cr.set_source_rgb(0.3 + 0.7 * (i / 8), 0.5, 0.8)
            bar_width = width / 8
            x = i * bar_width
            y = height - bar_height
            cr.rectangle(x + 2, y, bar_width - 4, bar_height)
            cr.fill()
        
        # Draw text
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.set_font_size(12)
        cr.move_to(10, height - 20)
        cr.show_text(f"Time: {time.time():.1f}")
        
        return True
    
    def update_visualizer(self):
        print(f"Update called at {time.time()}")
        if hasattr(self, 'drawing_area'):
            self.drawing_area.queue_draw()
        return True

class TestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.test.viz")
    
    def do_activate(self):
        print("Activating app...")
        win = TestVisualization(self)
        win.present()
        print("Window presented")

if __name__ == "__main__":
    import math
    print("Starting test app...")
    app = TestApp()
    app.run()
