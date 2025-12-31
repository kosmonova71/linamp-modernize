#!/usr/bin/env python3
import sys
import os

# Add current directory to path
sys.path.insert(0, '/home/shadowyfigure/Documents')

print("Starting browser test...")

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
    from gi.repository import Gtk, WebKit
    print("GTK/WebKit imports successful")
    
    # Try to import the browser module
    import lightbroswer
    print("Module import successful")
    
    # Try to create the app
    app = lightbroswer.ShadowBrowser()
    print("App created successfully")
    
    # Try to run
    print("Attempting to run app...")
    app.run(None)
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
