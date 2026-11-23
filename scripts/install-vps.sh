#!/bin/bash
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: install-vps.sh init|upgrade [--dry-run] [--caddy | --caddy-site FILE]'
}
fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

mode=${1:-}
case "$mode" in
  init|upgrade) shift ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
dry_run=false
manage_caddy=false
site_source=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --caddy) manage_caddy=true; shift ;;
    --caddy-site)
      [ "$#" -ge 2 ] || fail '--caddy-site requires a path'
      manage_caddy=true
      site_source=$(realpath -e -- "$2")
      [ -f "$site_source" ] || fail 'Caddy site source must be a file'
      shift 2 ;;
    *) usage >&2; exit 2 ;;
  esac
done

app_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$app_dir"
[ -f release.toml ] || fail 'Run the installer from an extracted release archive (make release)'
commit=$(sed -nE 's/^commit = "([0-9a-f]{40}|[0-9a-f]{64})"$/\1/p' release.toml)
[ -n "$commit" ] || fail 'The archive is missing a valid release commit'
if [ -z "$site_source" ]; then
  if [ -f /etc/caddy/newspaper.caddy ]; then
    site_source=/etc/caddy/newspaper.caddy
  else
    site_source="$app_dir/deploy/Caddyfile"
  fi
fi
units=(newspaper.service newspaper.timer newspaper-clean.service newspaper-clean.timer
       newspaper-notify.service newspaper-notify.timer newspaper-publish.service newspaper-publish.timer
       newspaper-translate.service newspaper-translate.timer)
for unit in "${units[@]}"; do
  [ -f "deploy/$unit" ] || fail "The release is missing deploy/$unit"
done

if "$dry_run"; then
  printf 'Mode: %s\nApplication: %s\nCommit: %s\n' "$mode" "$app_dir" "$commit"
  printf 'Install systemd unit: %s\n' "${units[@]}"
  if [ "$mode" = init ]; then
    printf '%s\n' 'Initialize service user, state directories and missing /etc/newspaper settings'
  else
    printf '%s\n' 'Use existing /etc/newspaper settings and state directories'
  fi
  if "$manage_caddy"; then
    printf 'Validate /etc/caddy/Caddyfile with site source %s, then install /etc/caddy/newspaper.caddy\n' "$site_source"
  fi
  exit 0
fi

[ "$EUID" -eq 0 ] || fail 'Run the installer as root'
[[ "$app_dir" =~ ^/opt/[A-Za-z0-9._/-]+$ ]] || fail 'Extract the release into a path under /opt with no spaces'
[ -x .venv/bin/python ] && [ -x node_modules/.bin/dsh ] || fail 'Run make install from the extracted release first'
PATH=/usr/local/bin:/usr/bin:/bin make check-node
if systemctl is-active --quiet "${units[@]}"; then
  fail 'Stop active Newspaper services and timers before installing'
fi

if [ "$mode" = init ]; then
  [ ! -f /etc/systemd/system/newspaper.service ] || fail 'An installation exists; use upgrade'
else
  id newspaper >/dev/null 2>&1 || fail 'The newspaper service user is missing; use init for a fresh installation'
  [ -f /etc/newspaper/config.toml ] || fail 'Missing existing /etc/newspaper/config.toml'
fi

unit_stage=
caddy_stage=
caddy_combined=
cleanup() {
  if [ -n "$unit_stage" ]; then rm -f -- "$unit_stage"; fi
  if [ -n "$caddy_stage" ]; then rm -f -- "$caddy_stage"; fi
  if [ -n "$caddy_combined" ]; then rm -f -- "$caddy_combined"; fi
}
trap cleanup EXIT
if "$manage_caddy"; then
  getent group caddy >/dev/null || fail 'Caddy group is missing'
  import_pattern='^[[:space:]]*import[[:space:]]+/etc/caddy/newspaper\.caddy[[:space:]]*(#.*)?$'
  [ -f /etc/caddy/Caddyfile ] || fail 'Configure /etc/caddy/Caddyfile with the Newspaper site import first'
  [ "$(grep -Ec "$import_pattern" /etc/caddy/Caddyfile || true)" = 1 ] ||
    fail 'Add exactly one top-level line: import /etc/caddy/newspaper.caddy'
  [ ! -L /etc/caddy/newspaper.caddy ] || fail 'Expected a regular Caddy site fragment'
  if [ "$site_source" = /etc/caddy/newspaper.caddy ]; then
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  else
    caddy_stage=$(mktemp /etc/caddy/.newspaper-site.XXXXXXXX)
    caddy_combined=$(mktemp /etc/caddy/.newspaper-combined.XXXXXXXX)
    install -m 644 "$site_source" "$caddy_stage"
    sed -E "s|${import_pattern}|import $caddy_stage|" /etc/caddy/Caddyfile > "$caddy_combined"
    caddy validate --config "$caddy_combined" --adapter caddyfile
  fi
fi

if [ "$mode" = init ]; then
  if ! id newspaper >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/newspaper --create-home --shell /usr/sbin/nologin newspaper
  fi
  install -d -o root -g newspaper -m 750 /etc/newspaper
  install -d -o newspaper -g newspaper -m 750 /var/lib/newspaper
  if ! "$manage_caddy"; then
    install -d -o newspaper -g newspaper -m 2750 /srv/newspaper /srv/newspaper/releases
  fi
  for entry in config:config.example.toml project.local:project.example.toml accounts.local:accounts.example.toml; do
    name=${entry%:*}
    source=${entry#*:}
    if [ ! -e "/etc/newspaper/$name.toml" ]; then
      install -o root -g newspaper -m 640 "$source" "/etc/newspaper/$name.toml"
    fi
  done
  if [ ! -e /etc/newspaper/credentials.env ]; then
    install -o root -g newspaper -m 640 .env.example /etc/newspaper/credentials.env
  fi
fi
if "$manage_caddy"; then
  install -d -o newspaper -g caddy -m 2750 /srv/newspaper /srv/newspaper/releases
  usermod -a -G caddy newspaper
fi

for unit in "${units[@]}"; do
  unit_stage=$(mktemp /etc/systemd/system/.newspaper-unit.XXXXXXXX)
  sed "s|/opt/Newspaper|$app_dir|g" "deploy/$unit" > "$unit_stage"
  chmod 644 "$unit_stage"
  mv -f -- "$unit_stage" "/etc/systemd/system/$unit"
  unit_stage=
done
if [ -n "$caddy_stage" ]; then
  mv -f -- "$caddy_stage" /etc/caddy/newspaper.caddy
  caddy_stage=
fi
systemctl daemon-reload
printf '%s\n' 'Installed. Review the settings before starting services and enabling timers.'
