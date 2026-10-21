#!/usr/bin/env bash
# Deploys Pulseboard Profile A to Cloud Foundry (ADDENDUM-01 §1A.8).
#
# Usage (from the repository root, logged in with `cf login` and targeting the space):
#   deploy/cloudfoundry/deploy.sh first      <image>   first deploy: push without starting
#   deploy/cloudfoundry/deploy.sh bluegreen  <image>   update: green next to blue, swap routes
#   deploy/cloudfoundry/deploy.sh rollback             route back to the kept blue app
#
# <image> is an exact tag such as ghcr.io/sunishbharat/pulseboard:1.2.0; latest is refused.
# Settings come from deploy/cloudfoundry/vars.yml (copy vars.example.yml). Environment:
#   VARS_FILE        another vars file (default deploy/cloudfoundry/vars.yml)
#   SECRET_SERVICE   user-provided or CredHub service holding the access keys, bound to green
#   CF_DOCKER_USERNAME + CF_DOCKER_PASSWORD   for a private registry (read by cf push)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${HERE}/manifest.yml"
VARS_FILE="${VARS_FILE:-${HERE}/vars.yml}"

die() { echo "error: $*" >&2; exit 1; }

var() {
    # Reads a top-level "key: value" from the vars file.
    sed -n "s/^$1:[[:space:]]*//p" "${VARS_FILE}" | head -n 1 | tr -d "\"'\r"
}

python_bin() {
    command -v python3 || command -v python || die "python is needed to copy environment variables"
}

check_image() {
    local image="${1:-}"
    [ -n "${image}" ] || die "give the image with an exact tag, for example ghcr.io/sunishbharat/pulseboard:1.2.0"
    case "${image##*/}" in
        *:latest) die "refusing the latest tag; use an exact release tag (CF-9)" ;;
        *:*) ;;
        *) die "the image needs an exact tag: ${image}" ;;
    esac
}

split_route() {
    # "host.domain[/path]" -> ROUTE_HOST, ROUTE_DOMAIN, ROUTE_PATH
    local route path
    route="$(var route)"
    [ -n "${route}" ] || die "set route in ${VARS_FILE}"
    path=""
    case "${route}" in */*) path="/${route#*/}"; route="${route%%/*}" ;; esac
    ROUTE_HOST="${route%%.*}"
    ROUTE_DOMAIN="${route#*.}"
    ROUTE_PATH="${path}"
}

map_route() {
    if [ -n "${ROUTE_PATH}" ]; then
        cf "$1" "$2" "${ROUTE_DOMAIN}" --hostname "${ROUTE_HOST}" --path "${ROUTE_PATH}"
    else
        cf "$1" "$2" "${ROUTE_DOMAIN}" --hostname "${ROUTE_HOST}"
    fi
}

app_exists() { cf app "$1" >/dev/null 2>&1; }

copy_env() {
    # Copies every user-provided variable from $1 to $2 through the v3 API (values may hold
    # any characters, which parsing `cf env` output would mangle).
    local from_guid to_guid body
    from_guid="$(cf app "$1" --guid)"
    to_guid="$(cf app "$2" --guid)"
    body="$(cf curl "/v3/apps/${from_guid}/environment_variables" \
        | "$(python_bin)" -c 'import json, sys; print(json.dumps({"var": json.load(sys.stdin)["var"]}))')"
    cf curl -X PATCH "/v3/apps/${to_guid}/environment_variables" -d "${body}" >/dev/null
    echo "Copied environment variables: $1 -> $2"
}

first() {
    local image="$1" app
    check_image "${image}"
    app="$(var app_name)"
    cf push "${app}" -f "${MANIFEST}" --vars-file "${VARS_FILE}" --var "image=${image}" --no-start
    cat <<EOF

Pushed ${app} without starting it. Next (ADDENDUM-01 §1A.8 step 4):
  cf set-env ${app} PULSEBOARD_SITE_URL https://<your-site>.atlassian.net
  cf set-env ${app} PULSEBOARD_SITE_DEPLOYMENT cloud
  cf set-env ${app} PULSEBOARD_PUBLIC_URL https://$(var route)
  cf set-env ${app} PULSEBOARD_ACCESS_KEYS '<key-1>,<key-2>'   # or bind a secret service
  cf start ${app}
After any later cf set-env, run: cf restage ${app}
EOF
}

bluegreen() {
    local image="$1" app green blue
    check_image "${image}"
    app="$(var app_name)"
    green="${app}-green"
    blue="${app}-blue"
    split_route
    app_exists "${app}" || die "${app} does not exist; run the first deploy"

    # 1. Push green without starting and without the live route.
    app_exists "${green}" && die "${green} exists from an unfinished deploy; delete it first"
    cf push "${green}" -f "${MANIFEST}" --vars-file "${VARS_FILE}" --var "image=${image}" \
        --no-start --no-route

    # 2. Same settings and secret store as blue.
    copy_env "${app}" "${green}"
    if [ -n "${SECRET_SERVICE:-}" ]; then
        cf bind-service "${green}" "${SECRET_SERVICE}"
    fi

    # 3. Start green; cf start returns only once the /health check passes.
    cf start "${green}"

    # 4. Swap routes.
    map_route map-route "${green}"
    map_route unmap-route "${app}"

    # 5. Stop blue and keep it for rollback; green takes the app name.
    cf stop "${app}"
    if app_exists "${blue}"; then
        cf delete "${blue}" -f
    fi
    cf rename "${app}" "${blue}"
    cf rename "${green}" "${app}"
    echo "Live: ${app} (${image}). Previous version kept, stopped, as ${blue}."
}

rollback() {
    local app blue
    app="$(var app_name)"
    blue="${app}-blue"
    split_route
    app_exists "${blue}" || die "no ${blue} to roll back to"
    cf start "${blue}"
    map_route map-route "${blue}"
    map_route unmap-route "${app}"
    cf stop "${app}"
    echo "Route now serves ${blue}; ${app} is stopped."
}

[ -f "${VARS_FILE}" ] || die "missing ${VARS_FILE}; copy ${HERE}/vars.example.yml and fill it in"

case "${1:-}" in
    first) first "${2:-}" ;;
    bluegreen) bluegreen "${2:-}" ;;
    rollback) rollback ;;
    *) sed -n '2,15p' "$0"; exit 2 ;;
esac
