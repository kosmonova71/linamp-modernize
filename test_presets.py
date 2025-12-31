#!/usr/bin/env python3

import sys
sys.path.append('.')

# Import the visualizer class
try:
    from Gamp import ProjectMVisualizer
    
    # Create a test instance
    print("Testing preset loading...")
    viz = ProjectMVisualizer()
    
    print(f"Available presets: {len(viz.available_presets)}")
    print(f"Categories: {list(viz.preset_categories.keys())}")
    
    if viz.available_presets:
        print(f"First few presets: {viz.available_presets[:5]}")
        for category, presets in viz.preset_categories.items():
            print(f"  {category}: {len(presets)} presets")
    else:
        print("No presets loaded!")
        
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
