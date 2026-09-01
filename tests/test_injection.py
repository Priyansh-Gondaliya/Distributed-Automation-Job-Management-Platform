import sys
sys.path.append(r'c:\Users\varun.rajput\Desktop\Priyansh\Epaper\Flask_run_file\worker_agent')
from worker import update_script_days_in_file
import re

script_path = r'C:\Automation\scripts\EpaperSandesh_com_Ahmedabad.py'

print("Test regex match for days:")
with open(script_path, 'r', encoding='utf-8') as f:
    content = f.read()

match = re.search(r'^days\s*=\s*(\d+)', content, re.MULTILINE | re.IGNORECASE)
if match:
    print(f"Found days = {match.group(1)}")
else:
    print("days variable NOT found by regex!")

print("Running update_script_days_in_file with days=5...")
res = update_script_days_in_file(script_path, 5)
print(res)

with open(script_path, 'r', encoding='utf-8') as f:
    content2 = f.read()

match2 = re.search(r'^days\s*=\s*(\d+)', content2, re.MULTILINE | re.IGNORECASE)
if match2:
    print(f"After update, days = {match2.group(1)}")

# Revert it manually
import shutil
# We'll just run update again to put it back to 0
update_script_days_in_file(script_path, 0)
print("Reverted days back to 0")
