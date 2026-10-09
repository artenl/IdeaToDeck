#!/usr/bin/env bash
# IdeaToDeck one-line installer / updater for a Debian or Ubuntu VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/artenl/IdeaToDeck/HEAD/install.sh | sudo bash
#
# It installs Docker if needed, clones the repo to /opt/isthisideagood, asks for your
# API keys and an admin login, and starts the app (with automatic HTTPS when you
# give it a domain). Re-run the same command to update.
#
# Non-interactive use: pass answers as environment variables after `sudo`, e.g.
#   curl -fsSL .../install.sh | sudo ANTHROPIC_API_KEY=sk-ant-... TAVILY_API_KEY=tvly-... \
#     ADMIN_EMAIL=me@example.com ADMIN_PASSWORD='...' DOMAIN=ideas.example.com bash
#
# Other variables: INSTALL_DIR (default /opt/isthisideagood, the project's original
# name, kept so existing installs update in place), REPO_URL, BRANCH,
# RECONFIGURE=1 (ask everything again), HTTP_PORT (direct mode port, default 8080),
# CHECK_KEYS=0 (skip the live Anthropic key check).

# Everything runs inside main() so bash reads the whole script before executing it;
# otherwise commands reading stdin could swallow the rest of a piped script.
main() {
  set -Eeuo pipefail
  trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

  REPO_URL="${REPO_URL:-https://github.com/artenl/IdeaToDeck.git}"
  BRANCH="${BRANCH:-}"
  INSTALL_DIR="${INSTALL_DIR:-/opt/isthisideagood}"
  HTTP_PORT="${HTTP_PORT:-8080}"
  RECONFIGURE="${RECONFIGURE:-0}"
  ENV_FILE="$INSTALL_DIR/.env"

  banner
  [ "$(id -u)" -eq 0 ] || die "Please run as root, e.g.: curl -fsSL <url> | sudo bash"

  install_packages
  install_docker
  fetch_code

  if [ -f "$ENV_FILE" ] && [ "$RECONFIGURE" != "1" ]; then
    step "Keeping existing configuration ($ENV_FILE)"
    say "  Run with RECONFIGURE=1 to change keys, domain or admin."
    load_env_value DOMAIN
    load_env_value APP_PORT
    load_env_value INSTALL_MODE
    MODE=${INSTALL_MODE:-direct}
  else
    configure
    write_env
  fi

  start_app
  if [ -n "${ADMIN_EMAIL:-}" ]; then
    create_admin
  fi
  summary
}

# ---------------------------------------------------------------- helpers

if [ -t 2 ]; then
  C_G=$'\033[1;32m' C_Y=$'\033[1;33m' C_R=$'\033[1;31m' C_C=$'\033[1;36m' C_0=$'\033[0m'
else
  C_G="" C_Y="" C_R="" C_C="" C_0=""
fi

say() { printf '%s\n' "$*" >&2; }
step() { printf '\n%s▶ %s%s\n' "$C_G" "$*" "$C_0" >&2; }
warn() { printf '%s! %s%s\n' "$C_Y" "$*" "$C_0" >&2; }
die() { printf '%s✗ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

banner() {
  printf '%s' "$C_C" >&2
  cat >&2 <<'EOF'

  ___ ___  ___   _      __ __  ___  ___  ___ _  __
 |_ _|   \| __| /_\    / // / |   \| __|/ __| |/ /
  | || |) | _| / _ \  / // /  | |) | _|| (__| ' <
 |___|___/|___/_/ \_\/_//_/   |___/|___|\___|_|\_\
        idea to deck // installer
EOF
  printf '%s\n' "$C_0" >&2
}

have_tty() { { : </dev/tty; } 2>/dev/null; }

# ask VAR "Prompt" [default] [flags]: flags may contain "secret" and/or "optional".
# Keeps VAR if it is already set in the environment.
ask() {
  local var=$1 prompt=$2 default=${3:-} flags=${4:-} reply=""
  if [ -n "${!var:-}" ]; then return 0; fi
  if ! have_tty; then
    printf -v "$var" '%s' "$default"
    if [ -n "$default" ] || [[ "$flags" == *optional* ]]; then return 0; fi
    die "$var is required and there is no terminal to ask. Pass it as an environment variable."
  fi
  if [[ "$flags" == *secret* ]]; then
    read -r -s -p "  $prompt: " reply </dev/tty
    printf '\n' >&2
  else
    read -r -p "  $prompt${default:+ [$default]}: " reply </dev/tty
  fi
  printf -v "$var" '%s' "${reply:-$default}"
}

load_env_value() {
  local key=$1 line
  line=$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 || true)
  printf -v "$key" '%s' "${line#*=}"
}

port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# Names of what listens on 80/443: host programs, plus Docker containers publishing them.
port_holders() {
  local procs containers
  procs=$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null \
    | grep -o 'users:(("[^"]*"' | cut -d'"' -f2 | grep -v '^docker-proxy$' | sort -u | tr '\n' ' ' || true)
  containers=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
    | grep -E ':(80|443)->' | awk '{print $1}' | grep -v '^isthisideagood-' | sort -u | tr '\n' ' ' || true)
  printf '%s' "${procs}${containers:+docker containers: $containers}"
}

our_caddy_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^isthisideagood-caddy'
}

public_ip() {
  hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -vE '^(10|127|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.' | head -n1
}

compose() { docker compose --project-directory "$INSTALL_DIR" "$@"; }

# ---------------------------------------------------------------- steps

install_packages() {
  step "Checking system packages"
  local missing=()
  for cmd in git curl openssl ss; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if [ ${#missing[@]} -eq 0 ]; then say "  all present"; return; fi
  command -v apt-get >/dev/null 2>&1 || die "Missing ${missing[*]} and no apt-get. Install them and re-run."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq </dev/null
  apt-get install -y -qq git curl ca-certificates openssl iproute2 </dev/null >/dev/null
}

install_docker() {
  step "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    say "  installing Docker (get.docker.com)…"
    local script
    script=$(mktemp)
    curl -fsSL https://get.docker.com -o "$script"
    sh "$script" </dev/null >/dev/null
    rm -f "$script"
  fi
  if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  docker compose version >/dev/null 2>&1 || {
    command -v apt-get >/dev/null 2>&1 && apt-get install -y -qq docker-compose-plugin </dev/null >/dev/null
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose)."
  }
  say "  $(docker --version)"
}

fetch_code() {
  step "Fetching code into $INSTALL_DIR"
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --quiet origin
    if [ -n "$BRANCH" ]; then git -C "$INSTALL_DIR" checkout --quiet "$BRANCH"; fi
    git -C "$INSTALL_DIR" pull --quiet --ff-only \
      || die "Could not fast-forward $INSTALL_DIR (local changes?). Resolve them and re-run."
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    if [ -n "$BRANCH" ]; then
      git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    else
      git clone --quiet "$REPO_URL" "$INSTALL_DIR"
    fi
  fi
  say "  at $(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)@$(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
}

configure() {
  step "Configuration"
  say "  Keys stay on this server in $ENV_FILE (readable by root only)."
  say "  Anthropic key: https://console.anthropic.com/settings/keys"
  say "  Tavily key (1,000 free searches/month): https://app.tavily.com"
  say "  Press Enter to skip a key and add it later in the app (KEYS button, top bar)."
  ask ANTHROPIC_API_KEY "Anthropic API key" "" "secret optional"
  if [ -n "$ANTHROPIC_API_KEY" ]; then
    check_anthropic_key
  else
    warn "No Anthropic key yet. Add it in the app with the KEYS button."
  fi
  ask TAVILY_API_KEY "Tavily API key" "" "secret optional"
  if [ -z "$TAVILY_API_KEY" ]; then
    warn "No Tavily key yet. Add it in the app with the KEYS button."
  elif [[ "$TAVILY_API_KEY" != tvly-* ]]; then
    warn "Tavily keys usually start with 'tvly-'."
  fi

  say ""
  say "  Admin account (unlimited runs). Anyone else who signs in sees 'Coming soon'."
  ask ADMIN_EMAIL "Admin email"
  ADMIN_EMAIL=$(printf '%s' "$ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  [[ "$ADMIN_EMAIL" == *@*.* ]] || die "That doesn't look like an email address."
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    local confirm=""
    ask ADMIN_PASSWORD "Admin password (min 8 chars)" "" secret
    if have_tty; then
      read -r -s -p "  Repeat password: " confirm </dev/tty
      printf '\n' >&2
      [ "$confirm" = "$ADMIN_PASSWORD" ] || die "Passwords do not match."
    fi
  fi
  [ ${#ADMIN_PASSWORD} -ge 8 ] || die "Password must be at least 8 characters."

  say ""
  local guess=""
  guess=$(hostname -f 2>/dev/null || true)
  [[ "$guess" == *.* && "$guess" != localhost* ]] || guess=""
  say "  A domain pointing at this server gives you automatic HTTPS."
  say "  Leave it empty to serve plain HTTP on port $HTTP_PORT (not recommended)."
  ask DOMAIN "Domain (or - for none)" "$guess" optional
  DOMAIN=$(printf '%s' "${DOMAIN:-}" | tr -d '[:space:]')
  [ "$DOMAIN" = "-" ] && DOMAIN=""

  if [ -n "$DOMAIN" ]; then
    if (port_busy 80 || port_busy 443) && ! our_caddy_running; then
      warn "Ports 80/443 are already used by: $(port_holders)"
      warn "The app will only listen on 127.0.0.1:8000, so it is not reachable from outside yet."
      MODE=behind-proxy
    else
      MODE=https
      check_dns
    fi
  else
    MODE=direct
  fi
}

# Sends one tiny message: listing models works even with zero credits, this doesn't.
check_anthropic_key() {
  local out code msg
  if [ "${CHECK_KEYS:-1}" = "0" ]; then return 0; fi
  out=$(curl -s -w '\n%{http_code}' https://api.anthropic.com/v1/messages \
    -H @<(printf 'x-api-key: %s\n' "$ANTHROPIC_API_KEY") \
    -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
    -d '{"model":"claude-haiku-5-5","max_tokens":32,"messages":[{"role":"user","content":"Reply with OK."}]}' \
    || true)
  code=${out##*$'\n'}
  msg=$(printf '%s' "${out%$'\n'*}" | sed -n 's/.*"message":"\([^"]*\)".*/\1/p' | head -n1)
  case "$code" in
    200) say "  Anthropic key OK" ;;
    401) die "Anthropic rejected that key." ;;
    *)
      if [[ "$msg" == *"credit balance"* ]]; then
        warn "The Anthropic key is valid but the account has no credits."
        warn "Add some at console.anthropic.com > Settings > Billing (no reinstall needed)."
      else
        warn "Could not fully verify the Anthropic key (HTTP $code${msg:+: $msg}); continuing."
      fi
      ;;
  esac
}

check_dns() {
  local resolved ip
  resolved=$( (getent ahostsv4 "$DOMAIN" 2>/dev/null || true) | awk 'NR==1 {print $1}')
  ip=$(public_ip || true)
  if [ -z "$resolved" ]; then
    warn "$DOMAIN does not resolve yet. Create an A record pointing to ${ip:-this server}."
  elif [ -n "$ip" ] && [ "$resolved" != "$ip" ]; then
    warn "$DOMAIN points to $resolved but this server looks like $ip. HTTPS will fail until DNS is fixed."
  fi
}

write_env() {
  step "Writing $ENV_FILE"
  local profiles="" bind="127.0.0.1" port="8000" secure="true" forwarded="*"
  case "$MODE" in
    https) profiles="https" ;;
    behind-proxy) ;;
    direct) bind="0.0.0.0"; port="$HTTP_PORT"; secure="false"; forwarded="127.0.0.1" ;;
  esac
  APP_PORT=$port
  local secret
  secret=$(openssl rand -hex 32)
  (
    umask 077
    {
      echo "# Written by install.sh on $(date -u +%Y-%m-%dT%H:%MZ). Re-run the installer to update."
      echo "# After editing, apply with: cd $INSTALL_DIR && docker compose up -d"
      echo "INSTALL_MODE=$MODE"
      echo "COMPOSE_PROJECT_NAME=isthisideagood"
      echo "COMPOSE_PROFILES=$profiles"
      echo "DOMAIN=$DOMAIN"
      echo "APP_BIND=$bind"
      echo "APP_PORT=$port"
      echo "SECURE_COOKIES=$secure"
      echo "FORWARDED_ALLOW_IPS=$forwarded"
      echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
      echo "TAVILY_API_KEY=$TAVILY_API_KEY"
      echo "SESSION_SECRET=$secret"
      echo "# Optional tuning (see .env.example): CHEAP_MODEL, DEEP_MODEL, MAX_QUERIES,"
      echo "# DEFAULT_USER_MONTHLY_RUNS, MONTHLY_BUDGET_USD"
    } >"$ENV_FILE"
  )
  chmod 600 "$ENV_FILE"
}

start_app() {
  step "Building and starting containers (first build takes a few minutes)"
  cd "$INSTALL_DIR"
  if [ "${MODE:-}" != "https" ]; then
    compose --profile https rm -sf caddy >/dev/null 2>&1 || true
  fi
  compose build app </dev/null
  compose up -d --remove-orphans </dev/null
  local port=${APP_PORT:-8000}
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
      say "  app is healthy"
      return
    fi
    sleep 2
  done
  compose logs --tail 40 app >&2 || true
  die "The app did not become healthy. Logs above."
}

create_admin() {
  step "Whitelisting $ADMIN_EMAIL as admin"
  [ -n "${ADMIN_PASSWORD:-}" ] || ask ADMIN_PASSWORD "Password for $ADMIN_EMAIL" "" secret
  printf '%s\n' "$ADMIN_PASSWORD" | compose exec -T app idea-eval users add "$ADMIN_EMAIL" --admin --password-stdin
  unset ADMIN_PASSWORD
}

summary() {
  local url ip
  ip=$(public_ip || true)
  case "${MODE:-direct}" in
    https) url="https://$DOMAIN" ;;
    behind-proxy) url="not public yet (see below)" ;;
    *) url="http://${ip:-<server-ip>}:${APP_PORT:-$HTTP_PORT}" ;;
  esac
  step "Done"
  cat >&2 <<EOF

  Open:            $url
  Update:          re-run the install command
  Add a user:      cd $INSTALL_DIR && docker compose exec app idea-eval users add friend@example.com --limit 10
  Admin password:  cd $INSTALL_DIR && docker compose exec app idea-eval users passwd you@example.com
  Waitlist:        cd $INSTALL_DIR && docker compose exec app idea-eval waitlist
  Logs:            cd $INSTALL_DIR && docker compose logs -f app

EOF
  if [ "${MODE:-}" = "behind-proxy" ]; then
    warn "Ports 80/443 belong to: $(port_holders)"
    cat >&2 <<EOF
  Use it now:      on your own computer run
                     ssh -N -L 8000:127.0.0.1:8000 ${SUDO_USER:-root}@${ip:-<server-ip>}
                   leave it running, then open http://localhost:8000 in Chrome
  Make it public:  either point the program on ports 80/443 at http://127.0.0.1:8000,
                   or stop that program and re-run the installer with RECONFIGURE=1
                   to get automatic HTTPS for $DOMAIN.
EOF
  elif [ "${MODE:-}" = "https" ]; then
    say "  Make sure ports 80 and 443 are open in your provider's firewall."
  elif [ "${MODE:-}" = "direct" ]; then
    warn "Plain HTTP sends your password unencrypted. Re-run with RECONFIGURE=1 and a domain to enable HTTPS."
    say "  Make sure port ${APP_PORT:-$HTTP_PORT} is open in your provider's firewall."
  fi
}

main "$@"
