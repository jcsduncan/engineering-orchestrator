import re
import sys

title = sys.argv[1]
pattern = r"(feat|fix|docs|chore|refactor|test|ci|build|perf|revert)(\([a-z0-9-]+\))?!?: .+"
if not re.fullmatch(pattern, title):
    raise SystemExit("PR title must use a Conventional Commit subject, e.g. 'fix: handle timeout'")
