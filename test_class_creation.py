#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/shadowyfigure/Documents')

print("Testing ShadowBrowser class creation...")

try:
    import lightbroswer
    print("Creating ShadowBrowser instance...")
    app = lightbroswer.ShadowBrowser()
    print("Class creation: OK")
except Exception as e:
    print(f"Class creation failed: {e}")
    import traceback
    traceback.print_exc()
