# JavaScript void(0) Link Fix Summary

## Problem
Links with `href="javascript:void(0)"` and `onclick="window.open(dbneg('...'), '_blank')"` were not working properly. The onclick handler was executing and opening a new window before the browser could intercept and handle it properly.

## Root Cause
The inline `onclick` attribute executes during the event's target phase, which happens BEFORE event listeners in the capture phase can prevent it. This means even with `addEventListener(..., true)`, we couldn't stop the onclick from executing.

## Solution
The fix removes the onclick attribute from void links and replaces it with our own event handler:

1. **Scan for void links** - Find all `<a href="javascript:void(0)">` elements
2. **Store original onclick** - Save the onclick attribute value to `data-original-onclick`
3. **Remove onclick** - Remove the onclick attribute to prevent it from executing
4. **Add custom handler** - Attach our own click event listener that:
   - Prevents default behavior
   - Parses the original onclick to extract URLs
   - Handles both direct URLs and function calls (like `dbneg()`)
   - Sends the extracted URL to Python via WebKit message handler

## Code Changes
**File:** `/home/shadowyfigure/Documents/browser/shadow/shadowbrowser.py`
**Lines:** 1379-1492 (custom_script in AdBlocker.__init__)

### Key Features
- **Pattern matching** for `window.open('url')` - extracts direct URLs
- **Function execution** for `window.open(dbneg('id'))` - calls the function and gets result
- **MutationObserver** - watches for dynamically added links
- **Prevents duplicate processing** - uses `data-void-processed` attribute

## Testing
Test file: `/home/shadowyfigure/Documents/test_void_links.html`

### Test Cases
1. Simple window.open with direct URL
2. window.open with dbneg() function (your sample code)
3. Regular href link (control)
4. Hash link with onclick
5. Direct URL in onclick

### How to Test
1. Restart ShadowBrowser to load the new code
2. Open `test_void_links.html` in the browser
3. Open Developer Tools (F12) to see console messages
4. Click each test link
5. Verify links open in new tabs (not popup windows)
6. Check console for `[VOID_HANDLER]` debug messages
7. Check terminal for Python `[VOID_LINK_CLICKED]` messages

## Expected Console Output
```
[VOID_HANDLER] Void link handler loaded
[VOID_HANDLER] Found 4 void links
[VOID_HANDLER] Processing link with onclick: window.open(dbneg('...'), '_blank')
[VOID_HANDLER] Click intercepted on void link
[VOID_HANDLER] Found function call: dbneg('...')
[VOID_HANDLER] Function result: https://www.decoded-example.com/...
[VOID_HANDLER] Sending URL to Python: https://www.decoded-example.com/...
```

## Next Steps
If the fix still doesn't work:
1. Check browser console for error messages
2. Verify the dbneg() function exists on the actual website
3. Check if the website uses a different function name
4. Look for Content Security Policy (CSP) restrictions
5. Verify WebKit message handlers are properly registered
