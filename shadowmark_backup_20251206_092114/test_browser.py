#!/usr/bin/env python3
import os
import sys
import subprocess
import time

# Set up logging environment
os.environ['SHADOW_BROWSER_LOG_LEVEL'] = 'DEBUG'

# Create log directory
log_dir = os.path.expanduser('~/.local/share/shadow-browser')
os.makedirs(log_dir, exist_ok=True)

print(f"Starting browser with logging to: {log_dir}/browser.log")

# Run the browser
try:
    result = subprocess.run([sys.executable, 'shadowmark2.py'], 
                          cwd='/home/shadowyfigure/Documents',
                          capture_output=True, 
                          text=True, 
                          timeout=10)  # Run for 10 seconds then kill
    
    print("STDOUT:")
    print(result.stdout)
    print("\nSTDERR:")
    print(result.stderr)
    print(f"\nReturn code: {result.returncode}")
    
except subprocess.TimeoutExpired:
    print("Browser started successfully (timed out after 10 seconds as expected)")
    
    # Check the log file
    log_file = os.path.join(log_dir, 'browser.log')
    if os.path.exists(log_file):
        print(f"\nLog file contents ({log_file}):")
        with open(log_file, 'r') as f:
            print(f.read())
    else:
        print("Log file not found")

except Exception as e:
    print(f"Error running browser: {e}")
