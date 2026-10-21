#!/bin/bash
set -euo pipefail
cd /opt/Newspaper
make check-node
if ! id newspaper >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/newspaper --create-home --shell /usr/sbin/nologin newspaper
fi
install -d -o root -g newspaper -m 750 /etc/newspaper
install -d -o newspaper -g newspaper -m 750 /var/lib/newspaper
install -d -o newspaper -g caddy -m 2750 /srv/newspaper /srv/newspaper/releases
usermod -a -G caddy newspaper
if [ ! -f /etc/newspaper/config.toml ]; then
  install -o root -g newspaper -m 640 config.example.toml /etc/newspaper/config.toml
fi
if [ ! -f /etc/newspaper/project.local.toml ]; then
  install -o root -g newspaper -m 640 project.example.toml /etc/newspaper/project.local.toml
fi
if [ ! -f /etc/newspaper/credentials.env ]; then
  install -o root -g newspaper -m 640 .env.example /etc/newspaper/credentials.env
fi
if [ ! -f /etc/newspaper/accounts.local.toml ]; then
  install -o root -g newspaper -m 640 accounts.example.toml /etc/newspaper/accounts.local.toml
fi
install -m 644 deploy/newspaper*.service deploy/newspaper*.timer /etc/systemd/system/
install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile
systemctl daemon-reload
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
echo 'Installed. Configure /etc/newspaper files, build the site, then enable timers.'
