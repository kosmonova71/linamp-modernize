#!/usr/bin/env python3

import os
import sys

# Test preset detection
print("Testing preset directory detection...")

home_dir = os.path.expanduser('~')
preset_dir = os.path.join(home_dir, '.projectM', 'presets')

print(f"Home directory: {home_dir}")
print(f"Preset directory: {preset_dir}")
print(f"Directory exists: {os.path.exists(preset_dir)}")

if os.path.exists(preset_dir):
    files = os.listdir(preset_dir)
    milk_files = [f for f in files if f.endswith('.milk')]
    print(f"Total files: {len(files)}")
    print(f"Milk files: {len(milk_files)}")
    print("First few files:", milk_files[:5])
else:
    print("Creating preset directory...")
    os.makedirs(preset_dir, exist_ok=True)
    print(f"Created: {preset_dir}")

# Test the path expansion logic from Gamp.py
possible_dirs = [
    "~/.projectM/presets",
    "/usr/share/projectM/presets",
    "/usr/local/share/projectM/presets"
]

found_dir = None
for dir_path in possible_dirs:
    expanded = os.path.expanduser(dir_path)
    print(f"Checking {dir_path} -> {expanded}: {os.path.exists(expanded)}")
    if os.path.exists(expanded):
        found_dir = expanded
        break

print(f"Found directory: {found_dir}")
