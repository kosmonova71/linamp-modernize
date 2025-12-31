#!/usr/bin/env python3

import cairo
import math
import time

# Test Cairo drawing
width, height = 400, 300
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
cr = cairo.Context(surface)

# Clear background
cr.set_source_rgb(0.2, 0.1, 0.3)
cr.paint()

# Draw test rectangle
cr.set_source_rgb(1.0, 1.0, 0.0)
cr.rectangle(10, 10, 50, 50)
cr.fill()

# Draw animated bars
time_factor = time.time() * 2.0
for i in range(8):
    height_factor = 0.3 + 0.2 * math.sin(time_factor + i * 0.5)
    bar_height = height * height_factor * 0.8
    
    cr.set_source_rgb(0.3 + 0.7 * (i / 8), 0.5, 0.8)
    bar_width = width / 8
    x = i * bar_width
    y = height - bar_height
    cr.rectangle(x + 2, y, bar_width - 4, bar_height)
    cr.fill()

# Save to file
surface.write_to_png("/tmp/test_visualization.png")
print("Visualization test saved to /tmp/test_visualization.png")
