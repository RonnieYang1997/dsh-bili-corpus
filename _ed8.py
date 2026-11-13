import io

p = 'src/index.ts'
s = io.open(p, encoding='utf-8').read()

a = """      applied: Array.isArray(report.reports) ? report.reports.filter((r: any) => r && r.ok).length : null,"""
b = """      applied: Array.isArray(report.reports) ? report.reports.filter((r: any) => r && r.ok).length : null,
      unavailableDocs: llmSkipped,"""
assert s.count(a) == 1
s = s.replace(a, b, 1)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
print('ok')
