import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib

class TestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.TestApp")
        self.window = None

    def do_activate(self):
        print("Application activated")  # Debug print
        if not self.window:
            self.window = Gtk.ApplicationWindow(application=self, title="GTK4 Test")
            self.window.set_default_size(400, 200)
            
            label = Gtk.Label(label="GTK4 is working!")
            self.window.set_child(label)
            
            print("Window created and showing")  # Debug print
            self.window.present()
        else:
            print("Window already exists")  # Debug print

if __name__ == '__main__':
    print("Starting application")  # Debug print
    app = TestApp()
    print("Running application")  # Debug print
    exit_status = app.run(None)
    print(f"Application exited with status: {exit_status}")  # Debug print
