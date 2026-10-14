#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Do not add secrets or local credentials. .gitignore protects common sensitive files.
git add -A

if git diff --cached --quiet; then
  echo "Нет изменений для отправки в GitHub."
  exit 0
fi

STAMP="$(date -u '+%Y-%m-%d %H:%M UTC')"
git commit -m "Обновить футбольный отчёт — ${STAMP}"
git push origin main
printf 'Изменения отправлены в %s\n' "$(git rev-parse --short HEAD)"
