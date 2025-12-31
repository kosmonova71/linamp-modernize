import sys
import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
gi.require_version('GstAudio', '1.0')

from gi.repository import Gst, GLib, GObject

# Initialize GStreamer
Gst.init(None)

class TestGST:
    def __init__(self):
        print("Creating pipeline")
        self.pipeline = Gst.Pipeline()
        
        # Create elements
        self.source = Gst.ElementFactory.make("audiotestsrc", "source")
        self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
        
        if not self.source or not self.sink:
            print("ERROR: Could not create all elements")
            sys.exit(1)
            
        # Add elements to the pipeline
        self.pipeline.add(self.source)
        self.pipeline.add(self.sink)
        
        # Link elements
        if not self.source.link(self.sink):
            print("ERROR: Could not link elements")
            sys.exit(1)
            
        # Connect to the bus
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_message)
        
    def on_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            self.pipeline.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.EOS:
            print("End of stream")
            self.pipeline.set_state(Gst.State.NULL)
            
    def run(self):
        print("Starting pipeline")
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("ERROR: Unable to set the pipeline to the playing state")
            return
            
        # Run for 5 seconds
        print("Playing for 5 seconds...")
        GLib.timeout_add_seconds(5, self.stop)
        
        # Start the GLib main loop
        self.loop = GLib.MainLoop()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            print("Interrupted by user")
            self.pipeline.set_state(Gst.State.NULL)
            
    def stop(self):
        print("Stopping pipeline")
        self.pipeline.send_event(Gst.Event.new_eos())
        self.loop.quit()
        return False

if __name__ == "__main__":
    print("Starting GStreamer test")
    test = TestGST()
    test.run()
    print("Test completed")
