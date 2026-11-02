#!/usr/bin/env bash
# A daily, encrypted backup of KROVA's database, run on the EC2 server.
#
# What it does: dumps the database from the krova-postgres container, compresses it, encrypts it with
# BACKUP_PASSPHRASE (AES-256), keeps the last KEEP_DAYS days on the server in ~/krova-backups, and - if
# BACKUP_S3_BUCKET is set and the aws CLI works - copies each one to S3 too (so a lost server is not lost data).
#
# One-time setup on the server:
#   1. Put a long random passphrase in ~/krova-platform/.env:  BACKUP_PASSPHRASE=...
#      Keep a copy somewhere safe (a password manager). Without it a backup cannot be opened.
#   2. Optional, recommended: BACKUP_S3_BUCKET=your-bucket-name in .env (a private bucket in ap-south-1).
#   3. chmod +x ~/krova-platform/scripts/backup_db.sh
#   4. Run it once by hand:  ~/krova-platform/scripts/backup_db.sh
#   5. Every night at 2:30 AM:  (crontab -l 2>/dev/null; echo "30 2 * * * $HOME/krova-platform/scripts/backup_db.sh >> $HOME/krova-backups/backup.log 2>&1") | crontab -
#
# Restore (test this once, on a copy - a backup never restored is not a backup):
#   openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in FILE.sql.gz.enc | gunzip \
#     | docker exec -i krova-postgres psql -U krova -d krova
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-$(grep -E '^BACKUP_PASSPHRASE=' .env | cut -d= -f2- || true)}"
  BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-$(grep -E '^BACKUP_S3_BUCKET=' .env | cut -d= -f2- || true)}"
fi
: "${BACKUP_PASSPHRASE:?Set BACKUP_PASSPHRASE in .env first}"
export BACKUP_PASSPHRASE

KEEP_DAYS="${KEEP_DAYS:-14}"
DIR="$HOME/krova-backups"
mkdir -p "$DIR"
chmod 700 "$DIR"
FILE="$DIR/krova-$(date -u +%Y%m%d-%H%M).sql.gz.enc"

docker exec krova-postgres pg_dump -U krova -d krova --no-owner \
  | gzip -9 \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out "$FILE"
chmod 600 "$FILE"

SIZE=$(stat -c %s "$FILE")
if [ "$SIZE" -lt 1024 ]; then
  echo "$(date -u) backup looks empty ($SIZE bytes): $FILE" >&2
  exit 1
fi
echo "$(date -u) backup ok: $FILE ($SIZE bytes)"

if [ -n "${BACKUP_S3_BUCKET:-}" ] && command -v aws >/dev/null 2>&1; then
  aws s3 cp "$FILE" "s3://$BACKUP_S3_BUCKET/krova/$(basename "$FILE")" --sse AES256 --only-show-errors \
    && echo "$(date -u) copied to s3://$BACKUP_S3_BUCKET/krova/"
fi

find "$DIR" -name 'krova-*.sql.gz.enc' -mtime "+$KEEP_DAYS" -delete
