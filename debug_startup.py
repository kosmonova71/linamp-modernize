#!/usr/bin/env python3
import sys
import os

print("=== Debug Startup ===")
print(f"Python: {sys.version}")
print(f"Current dir: {os.getcwd()}")

# Test imports
print("\n1. Testing basic imports...")
try:
    import gi
    print("   gi: OK")
except ImportError as e:
    print(f"   gi: FAILED - {e}")
    sys.exit(1)

print("\n2. Testing GTK4...")
try:
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    print("   GTK4: OK")
except Exception as e:
    print(f"   GTK4: FAILED - {e}")
    sys.exit(1)

print("\n3. Testing WebKit2...")
try:
    gi.require_version('WebKit2', '4.0')
    from gi.repository import WebKit2
    print("   WebKit2: OK")
except Exception as e:
    print(f"   WebKit2: FAILED - {e}")
    try:
        gi.require_version('WebKit', '2.0')
        from gi.repository import WebKit
        print("   WebKit (legacy): OK")
    except Exception as e2:
        print(f"   WebKit (legacy): FAILED - {e2}")
        sys.exit(1)

print("\n4. Testing browser module import...")
try:
    sys.path.insert(0, '/home/shadowyfigure/Documents')
    import lightbroswer
    print("   lightbroswer: OK")
except Exception as e:
    print(f"   lightbroswer: FAILED - {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n5. Testing app creation...")
try:
    app = lightbroswer.ShadowBrowser()
    print("   App creation: OK")
except Exception as e:
    print(f"   App creation: FAILED - {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n=== All tests passed! ===")
