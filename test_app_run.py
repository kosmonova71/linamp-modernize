#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/shadowyfigure/Documents')

print("Testing app.run()...")

try:
    import lightbroswer
    print("Creating ShadowBrowser instance...")
    app = lightbroswer.ShadowBrowser()
    print("Running app (will timeout after 3 seconds)...")
    import signal
    def timeout_handler(signum, frame):
        print("App is running (timeout reached)")
        sys.exit(0)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(3)
    app.run(None)
    signal.alarm(0)
    print("App finished normally")
except Exception as e:
    print(f"App run failed: {e}")
    import traceback
    traceback.print_exc()
