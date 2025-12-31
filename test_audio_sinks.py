#!/usr/bin/env python3
"""
Simple test script to verify audio sink functionality
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)

def test_audio_sinks():
    """Test different audio sinks to find working ones"""
    sinks_to_try = [
        ("pulsesink", None),
        ("autoaudiosink", None), 
        ("alsasink", "default"),
        ("alsasink", "hw:1,0"),
        ("fakesink", None)
    ]
    
    for sink_name, device in sinks_to_try:
        try:
            print("Testing {sink_name}...".format(sink_name=sink_name), end=" ")
            
            # Create a simple test pipeline
            pipeline = Gst.Pipeline.new("test")
            source = Gst.ElementFactory.make("audiotestsrc", "source")
            sink = Gst.ElementFactory.make(sink_name, "sink")
            
            if not source or not sink:
                print(f"FAILED - Could not create elements")
                continue
                
            if device:
                sink.set_property("device", device)
            
            pipeline.add(source)
            pipeline.add(sink)
            source.link(sink)
            
            # Try to set to READY state
            ret = pipeline.set_state(Gst.State.READY)
            if ret == Gst.StateChangeReturn.SUCCESS:
                print("SUCCESS")
                pipeline.set_state(Gst.State.NULL)
                return sink_name, device
            else:
                print("FAILED - State change failed")
                pipeline.set_state(Gst.State.NULL)
                
        except Exception as e:
            print(f"FAILED - {e}")
            continue
    
    return None, None

if __name__ == "__main__":
    print("Testing audio sinks...")
    working_sink, device = test_audio_sinks()
    if working_sink:
        print(f"Working sink found: {working_sink}" + (f" with device: {device}" if device else ""))
    else:
        print("No working audio sink found")
