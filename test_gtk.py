#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

def on_activate(app):
    print("GTK app activated")
    window = Gtk.ApplicationWindow(application=app)
    window.set_title("Test")
    window.set_default_size(300, 200)
    window.present()
    print("Window presented")

app = Gtk.Application(application_id='com.example.test')
app.connect('activate', on_activate)
app.run(None)
