#!/usr/bin/env python3
print("Test starting...")

try:
    import sys
    print(f"Python version: {sys.version}")
    
    import os
    print(f"Display: {os.environ.get('DISPLAY', 'Not set')}")
    
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    print("GTK imported successfully")
    
    # Create a simple window
    win = Gtk.Window()
    win.set_title("Test Window")
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    
    print("Window created, starting main loop...")
    Gtk.main()
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
