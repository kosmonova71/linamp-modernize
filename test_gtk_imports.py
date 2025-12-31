#!/usr/bin/env python3
import gi

print("Testing GTK imports...")

try:
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    print("GTK3: OK")
except Exception as e:
    print(f"GTK3 failed: {e}")

try:
    gi.require_version("WebKit2", "4.0")
    from gi.repository import WebKit2
    print("WebKit2: OK")
except Exception as e:
    print(f"WebKit2 failed: {e}")

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    print("GStreamer: OK")
except Exception as e:
    print(f"GStreamer failed: {e}")

print("All GTK imports tested!")
