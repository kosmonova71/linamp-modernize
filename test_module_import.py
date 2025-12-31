#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/shadowyfigure/Documents')

print("Testing lightbroswer module import...")

try:
    import lightbroswer
    print("Module import: OK")
except Exception as e:
    print(f"Module import failed: {e}")
    import traceback
    traceback.print_exc()
