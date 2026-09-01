import re
with open('routes/web_routes.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(r"job[\'id\']", r"job['id']")
code = code.replace(r"script[\'worker_name\']", r"script['worker_name']")
code = code.replace(r"script[\'script_name\']", r"script['script_name']")
code = code.replace(r"script.get(\'script_path\', \'\')", r"script.get('script_path', '')")
code = code.replace(r"job.get(\'script_path\', \'\')", r"job.get('script_path', '')")
code = code.replace(r"sch[\'id\']", r"sch['id']")

with open('routes/web_routes.py', 'w', encoding='utf-8') as f:
    f.write(code)
print('Fixed backslashes')
