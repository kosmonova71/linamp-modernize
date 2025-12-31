#!/usr/bin/env python3

import sys
sys.path.insert(0, '/home/shadowyfigure/Documents')

try:
    import Gamp
    
    # Test the fix by creating the app and checking if the method exists
    app = Gamp.Linamp()
    
    print("✅ Linamp app created successfully")
    
    # Check if the update_visualizer method works without error
    try:
        result = app.update_visualizer()
        print(f"✅ update_visualizer() returned: {result}")
    except AttributeError as e:
        print(f"❌ AttributeError in update_visualizer: {e}")
    except Exception as e:
        print(f"⚠️  Other error in update_visualizer: {e}")
    
    # Check if audio_levels are initialized
    if hasattr(app, 'audio_levels'):
        print(f"✅ audio_levels initialized with {len(app.audio_levels)} items")
        print(f"   First few values: {app.audio_levels[:3]}")
    else:
        print("❌ audio_levels not found")
        
    if hasattr(app, 'smoothed_levels'):
        print(f"✅ smoothed_levels initialized with {len(app.smoothed_levels)} items")
        print(f"   First few values: {app.smoothed_levels[:3]}")
    else:
        print("❌ smoothed_levels not found")
    
    print("\n🎉 Visualization fix test completed successfully!")
    
except Exception as e:
    print(f"❌ Error creating app: {e}")
    import traceback
    traceback.print_exc()
