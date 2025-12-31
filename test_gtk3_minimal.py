#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

class TestApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Test GTK3")
        self.set_default_size(300, 200)
        self.connect("destroy", Gtk.main_quit)
        
        label = Gtk.Label(label="GTK3 Test - If you see this, GTK3 works!")
        self.add(label)
        
    def run(self):
        self.show_all()
        Gtk.main()

if __name__ == "__main__":
    app = TestApp()
    app.run()
