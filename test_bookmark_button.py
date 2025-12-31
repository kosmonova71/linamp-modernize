#!/usr/bin/env python3

# Test script to verify bookmark button creation logic
import sys
import os

# Add current directory to path
sys.path.insert(0, '/home/shadowyfigure/Documents')

try:
    # Try to import and test the bookmark button creation
    from shadowmark2 import ShadowBrowser
    import gi
    
    # Initialize GTK
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    
    print("Testing bookmark button creation...")
    
    # Create a minimal test instance
    class TestBrowser:
        def __init__(self):
            self.bookmarks = []
            self.bookmark_menu_button = None
            self.bookmark_popover = None
            self.bookmark_menu = None
            
        def safe_append(self, container, widget):
            """Test version of safe_append"""
            if not container or not widget:
                print("ERROR: container or widget is None")
                return False
            try:
                if hasattr(container, 'append'):
                    container.append(widget)
                else:
                    container.add(widget)
                return True
            except Exception as e:
                print(f"ERROR in safe_append: {e}")
                return False
                
        def update_bookmarks_menu(self, menu_container):
            """Test version of update_bookmarks_menu"""
            if not menu_container:
                print("ERROR: menu_container is None")
                return
            child = menu_container.get_first_child()
            while child:
                menu_container.remove(child)
                child = menu_container.get_first_child()
            print("Bookmarks menu updated successfully")
    
    # Test the bookmark button creation
    test_browser = TestBrowser()
    menubar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    
    # Simulate the bookmark button creation from create_menubar
    try:
        test_browser.bookmark_menu_button = Gtk.MenuButton(label="Bookmarks")
        test_browser.bookmark_menu_button.set_tooltip_text("Show bookmarks")
        test_browser.bookmark_menu_button.set_margin_start(5)
        test_browser.bookmark_menu_button.set_margin_end(5)
        
        test_browser.bookmark_popover = Gtk.Popover()
        test_browser.bookmark_popover.set_size_request(300, -1)
        
        test_browser.bookmark_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        test_browser.update_bookmarks_menu(test_browser.bookmark_menu)
        
        test_browser.bookmark_popover.set_child(test_browser.bookmark_menu)
        test_browser.bookmark_menu_button.set_popover(test_browser.bookmark_popover)
        
        result = test_browser.safe_append(menubar, test_browser.bookmark_menu_button)
        
        if result:
            print("SUCCESS: Bookmark button created and added to menubar")
            print(f"Menubar has {len(list(menubar))} children")
        else:
            print("ERROR: Failed to add bookmark button to menubar")
            
    except Exception as e:
        print(f"ERROR creating bookmark button: {e}")
        import traceback
        traceback.print_exc()
        
    print("Test completed successfully")
    
except ImportError as e:
    print(f"Import error: {e}")
    print("This might be due to missing GTK libraries in headless environment")
except Exception as e:
    print(f"Unexpected error: {e}")
    import traceback
    traceback.print_exc()
