import re
with open('templates/history.html', 'r', encoding='utf-8') as f:
    html = f.read()

html = html.replace("querySelectorAll('.tab-btn')", "querySelectorAll('.hist-nav-btn')")
html = html.replace("querySelector(`.tab-btn[onclick*=\"${tabId}\"]`)", "querySelector(`.hist-nav-btn[onclick*=\"${tabId}\"]`)")

with open('templates/history.html', 'w', encoding='utf-8') as f:
    f.write(html)
print('Fixed history.html script')
