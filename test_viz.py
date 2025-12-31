#!/usr/bin/env python3
print("Starting test...")
import sys
print("Python path:", sys.path[0])

try:
    import Gamp
    print("Gamp module imported")
    
    # Test basic initialization
    app = Gamp.Linamp()
    print("Linamp app created")
    
    # Check visualization data
    if hasattr(app, 'audio_levels'):
        print(f"audio_levels: {len(app.audio_levels)} items")
        print(f"First value: {app.audio_levels[0] if app.audio_levels else 'empty'}")
    else:
        print("audio_levels not found")
        
    if hasattr(app, 'smoothed_levels'):
        print(f"smoothed_levels: {len(app.smoothed_levels)} items")
        print(f"First value: {app.smoothed_levels[0] if app.smoothed_levels else 'empty'}")
    else:
        print("smoothed_levels not found")
        
    print("Test completed successfully")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
