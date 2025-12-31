# Shadowmark2.py Error Fixes Applied

## Issues Found and Fixed:

### 1. Duplicate Function Definitions
- **_register_webview_message_handlers**: Removed duplicate definition at lines 1297-1313
- **_clear_all_bookmarks**: First duplicate was already removed during edits
- **_on_delete_bookmark_clicked**: First duplicate was already removed during edits

### 2. Tor Port Assignment Bug
- **Location**: Line 879 in TorManager._start_new_tor_instance
- **Issue**: After correctly parsing the actual Tor SOCKS port, the code was incorrectly overwriting it with the default value 9050
- **Fix**: Removed the line `self.tor_port = 9050` that was incorrectly overriding the parsed port value

### 3. Code Formatting
- Fixed indentation issue after removing duplicate function

## Verification:
- Syntax check passed: `python3 -m py_compile shadowmark2.py` returns no errors
- Import test passed: Module can be imported successfully
- No structural issues detected

## Files Modified:
- `/home/shadowyfigure/Documents/shadowmark2.py`

## Notes:
All fixes maintain backward compatibility and do not change the functionality of the application. The duplicate function definitions were causing Python to only use the last definition, which could lead to unexpected behavior. The Tor port bug was preventing the application from using the actual Tor port configured in the system.
