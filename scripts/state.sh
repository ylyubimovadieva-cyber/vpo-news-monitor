#!/usr/bin/env bash
# Зберігання бази state/news.sqlite в окремій гілці «state» репозиторію.
# Гілка завжди містить один коміт (перезаписується), тож історія не росте.
#   bash scripts/state.sh pull   – завантажити базу перед запуском
#   bash scripts/state.sh push   – зберегти базу, якщо вона змінилася
# Якщо задано секрет STATE_KEY, база в гілці зберігається зашифрованою
# (AES-256), і її неможливо прочитати навіть у публічному репозиторії.
set -euo pipefail

BRANCH=state
DB=state/news.sqlite
SHA_FILE="${RUNNER_TEMP:-/tmp}/vpo_state.sha"

case "${1:-}" in
  pull)
    mkdir -p state
    if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      git fetch --quiet --depth=1 origin "$BRANCH"
      if [ -n "${STATE_KEY:-}" ] && git cat-file -e FETCH_HEAD:news.sqlite.enc 2>/dev/null; then
        git show FETCH_HEAD:news.sqlite.enc | openssl enc -d -aes-256-cbc -pbkdf2 -pass env:STATE_KEY -out "$DB"
      else
        git show FETCH_HEAD:news.sqlite > "$DB"
      fi
      echo "База завантажена з гілки $BRANCH"
    else
      echo "Гілки $BRANCH ще немає – буде створена нова база"
    fi
    if [ -f "$DB" ]; then sha256sum "$DB" | cut -d' ' -f1 > "$SHA_FILE"; else echo none > "$SHA_FILE"; fi
    ;;
  push)
    [ -f "$DB" ] || { echo "Бази немає – нічого зберігати"; exit 0; }
    new=$(sha256sum "$DB" | cut -d' ' -f1)
    old=$(cat "$SHA_FILE" 2>/dev/null || echo none)
    if [ "$new" = "$old" ]; then echo "База не змінилася"; exit 0; fi
    tmp=$(mktemp -d)
    if [ -n "${STATE_KEY:-}" ]; then
      openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:STATE_KEY -in "$DB" -out "$tmp/news.sqlite.enc"
      file=news.sqlite.enc
    else
      cp "$DB" "$tmp/news.sqlite"
      file=news.sqlite
    fi
    cd "$tmp"
    git init --quiet -b "$BRANCH"
    git add "$file"
    git -c user.name="vpo-news-bot" -c user.email="vpo-news-bot@users.noreply.github.com" \
        commit --quiet -m "Оновлення стану"
    git push --quiet --force "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$BRANCH:$BRANCH"
    echo "База збережена в гілку $BRANCH"
    ;;
  *)
    echo "Використання: $0 pull|push" >&2
    exit 2
    ;;
esac
