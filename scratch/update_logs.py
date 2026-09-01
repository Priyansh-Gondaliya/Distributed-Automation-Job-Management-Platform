import re

with open('routes/web_routes.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Fix 1: job_run (run_script)
code = re.sub(
    r'database\.log_action\(uid, "job_run", f"Queued job #\{job\[\'id\'\]\} for \{script\[\'worker_name\'\]\} / \{script\[\'script_name\'\]\}", _client_ip\(\), worker_name=script\["worker_name"\]\)',
    r'database.log_action(uid, "job_run", f"Queued job #{job[\'id\']} for {script[\'worker_name\']} / {script[\'script_name\']} ({script.get(\'script_path\', \'\')})", _client_ip(), worker_name=script["worker_name"])',
    code
)

# Fix 2: job_run (retry_job)
code = re.sub(
    r'database\.log_action\(uid, "job_run", f"Retried job #\{job_id\} \(\{script_n\}\) as new job #\{job\[\'id\'\]\}", _client_ip\(\), worker_name=job\["worker_name"\]\)',
    r'database.log_action(uid, "job_run", f"Retried job #{job_id} ({script_n}) ({job.get(\'script_path\', \'\')}) as new job #{job[\'id\']}", _client_ip(), worker_name=job["worker_name"])',
    code
)

# Fix 3: schedule_created
code = re.sub(
    r'database\.log_action\(\s*uid,\s*"schedule_created",\s*f"Created schedule #\{sch\[\'id\'\]\} for \{script\[\'script_name\'\]\}",\s*_client_ip\(\),\s*worker_name=script\["worker_name"\],\s*\)',
    r'''database.log_action(
            uid,
            "schedule_created",
            f"Created schedule #{sch['id']} for {script['script_name']} ({script.get('script_path', '')})",
            _client_ip(),
            worker_name=script["worker_name"],
        )''',
    code
)

# Fix 4: schedule_updated (there are a few, let's just make sure)
code = re.sub(
    r'database\.log_action\(\s*uid,\s*"schedule_updated",\s*f"Updated schedule #\{schedule_id\}",\s*_client_ip\(\),\s*worker_name=worker_name,\s*\)',
    r'''database.log_action(
            uid,
            "schedule_updated",
            f"Updated schedule #{schedule_id} for script ID {script_id}",
            _client_ip(),
            worker_name=worker_name,
        )''',
    code
)

with open('routes/web_routes.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Updated log_action calls in web_routes.py")
