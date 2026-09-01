import re

with open('app.py', 'r', encoding='utf-8') as f:
    app_py = f.read()

filter_code = '''
    import re
    from markupsafe import Markup

    @app.template_filter('format_details')
    def format_details(s):
        if not s:
            return ""
        # Escape first to prevent XSS
        from html import escape
        s = escape(str(s))
        
        # Highlight paths, IP addresses, and IDs (#123)
        # Match something that looks like a path or a filename with extension
        path_re = re.compile(r'((?:[a-zA-Z]:\\\\|/)[\\w\\\\/.-]+|\\b[\\w.-]+\\.(?:py|bat|exe|json|log|txt|csv|sql)\\b)')
        ip_re = re.compile(r'\\b(?:[0-9]{1,3}\\.){3}[0-9]{1,3}\\b')
        id_re = re.compile(r'#[0-9]+')
        
        s = path_re.sub(r'<span class="detail-highlight">\\1</span>', s)
        s = ip_re.sub(r'<span class="detail-highlight">\\g<0></span>', s)
        s = id_re.sub(r'<span class="detail-highlight">\\g<0></span>', s)
        
        return Markup(s)
'''

insert_pos = app_py.find("    @app.template_filter('b64encode')")
if insert_pos != -1:
    new_app = app_py[:insert_pos] + filter_code + "\n" + app_py[insert_pos:]
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(new_app)
    print("Added format_details filter to app.py")
else:
    print("Could not find insertion point")
