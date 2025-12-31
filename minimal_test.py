#!/usr/bin/env python3
print("Minimal test starting...")

# Test basic imports
import sys
print(f"Python path: {sys.path[:3]}")

# Test GTK
try:
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    print("GTK4 import: OK")
except ImportError as e:
    print(f"GTK4 import failed: {e}")
    sys.exit(1)

# Test WebKit
try:
    gi.require_version('WebKit', '6.0')
    from gi.repository import WebKit
    print("WebKit6 import: OK")
except ImportError as e:
    print(f"WebKit6 import failed: {e}")
    sys.exit(1)

# Test app creation
try:
    app = Gtk.Application(application_id='com.test.minimal')
    print("App creation: OK")
except Exception as e:
    print(f"App creation failed: {e}")
    sys.exit(1)

print("All tests passed!")
