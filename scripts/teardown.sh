#!/usr/bin/env bash
#
# ticket-to-pr-agent -- reverse scripts/setup.sh.
#
#   ./scripts/teardown.sh
#
# Removes what setup.sh created, roughly in reverse order, asking before every
# destructive step. The one departure from reverse order is deliberate: the hosted agent
# is offered for deletion BEFORE the PAT connection and the placeholder variable it
# depends on, because answering "no" to the agent after those are gone leaves a live agent
# whose ${{connections...}} no longer resolves.
#
# It only removes things setup.sh recorded creating: the azd environment carries
# T2PR_SETUP_* markers and this script reads them --
#
#   T2PR_SETUP_CREATED_PROJECT             the Foundry account, project, resource group
#   T2PR_SETUP_CREATED_MODEL_DEPLOYMENT    the model deployment
#   T2PR_SETUP_CREATED_ACR                 the container registry
#   T2PR_SETUP_GRANTED_ACRPULL_ACCOUNT     AcrPull for the Foundry account identity
#   T2PR_SETUP_GRANTED_ACRPULL_PROJECT     AcrPull for the Foundry project identity
#   T2PR_SETUP_CREATED_ENTRA_APP           the extension's app registration
#   T2PR_SETUP_CREATED_ADO_CONNECTION      the 'ado-secrets' Foundry connection
#   T2PR_SETUP_PAT_AUTHORIZATION_ID        the PAT setup.sh minted, for revocation
#
# A Foundry project you connected to rather than created is never deleted. Nothing is
# ever located by display name: names are not unique, and a name match is not an
# identification worth aiming `az ad app delete` at.
#
# Options:
#   --yes        answer yes to every prompt (dangerous; for scripted cleanup)
#   -h|--help    this text

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

ADO_RESOURCE_ID="499b84ac-1321-427f-aa17-267ca6975798"
ADO_CONNECTION_NAME="ado-secrets"
AZURE_YAML="azure.yaml"
ASSUME_YES=0

if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
else
  C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_OFF=''
fi

banner() { printf '\n%s=== %s ===%s\n' "${C_BOLD}" "$*" "${C_OFF}"; }
say()    { printf '  %s\n' "$*"; }
ok()     { printf '  %s-%s %s\n' "${C_GREEN}" "${C_OFF}" "$*"; }
skip()   { printf '  %s=%s %s\n' "${C_DIM}" "${C_OFF}" "$*"; }
warn()   { printf '  %s!%s %s\n' "${C_YELLOW}" "${C_OFF}" "$*" >&2; }
die()    { printf '\n%sFAILED:%s %s\n' "${C_RED}" "${C_OFF}" "$*" >&2; exit 1; }

# Prompts read from the controlling terminal so a piped stdin cannot silently answer
# them. With no terminal every prompt answers itself with "no" and nothing is deleted.
if { : </dev/tty; } 2>/dev/null; then PROMPT_SRC=/dev/tty; else PROMPT_SRC=/dev/stdin; fi

confirm() {
  [ "${ASSUME_YES}" -eq 0 ] || return 0
  local reply=''
  read -r -p "  ${C_YELLOW}?${C_OFF} $1 (y/N): " reply <"${PROMPT_SRC}" || true
  case "${reply}" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

jget() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for part in sys.argv[1].split("."):
    if part == "":
        continue
    if isinstance(data, list):
        try:
            data = data[int(part)]
        except (ValueError, IndexError):
            sys.exit(0)
    elif isinstance(data, dict):
        data = data.get(part)
    else:
        sys.exit(0)
    if data is None:
        sys.exit(0)
print(data if not isinstance(data, (dict, list)) else json.dumps(data))
' "$1"
}

AZD_VALUES_CACHE=''
azd_values_refresh() { AZD_VALUES_CACHE="$(azd env get-values -o json 2>/dev/null || echo '{}')"; }
env_get() { [ -n "${AZD_VALUES_CACHE}" ] || azd_values_refresh; printf '%s' "${AZD_VALUES_CACHE}" | jget "$1"; }

# The agent version resolves ${{connections.ado-secrets.credentials.pat}} at container
# start. Deleting that connection, or clearing the azd variable that carries the
# placeholder, while an agent version is still deployed leaves a live agent whose PAT no
# longer resolves -- it fails every run with ado_forbidden and nothing says why. The
# ordering in main() offers the agent for deletion first, so this only fires when the
# answer to that was "no".
agent_still_live_warning() {
  azd ai agent show >/dev/null 2>&1 || return 0
  warn "a hosted agent version is still deployed."
  say "It resolves \${{connections.${ADO_CONNECTION_NAME}.credentials.pat}} at container"
  say "start, so ${1} leaves it live with a PAT that no longer resolves: every run then"
  say "fails with ado_forbidden. Either delete the agent as well, or leave this in place."
}

role_unassign() {
  local principal="$1" role="$2" scope="$3"
  if [ -z "$(az role assignment list --assignee "${principal}" --scope "${scope}" \
              --query "[?roleDefinitionName=='${role}'].id" -o tsv 2>/dev/null)" ]; then
    skip "no '${role}' for ${principal}"
    return 0
  fi
  confirm "Remove '${role}' from ${principal} on ${scope##*/}" || { skip "kept ${role}"; return 0; }
  if az role assignment delete --assignee "${principal}" --role "${role}" --scope "${scope}" >/dev/null; then
    ok "removed ${role} from ${principal}"
  else
    warn "could not remove ${role} from ${principal} -- you may lack User Access Administrator"
  fi
}

# --------------------------------------------------------------------------------------
# 9 -- extension configuration
# --------------------------------------------------------------------------------------
undo9_extension_config() {
  banner "9  Extension configuration"
  # extension/src/agent-config.json is a tracked, deliberately empty template -- setup.sh
  # never writes it. The tenant-specific values live in gitignored build outputs: the two
  # files `npm run configure` writes, the dist/agent-config.json the build bundles from
  # them, and the .vsix setup.sh step 9 packages, which carries a copy of all of it.
  #
  # The .vsix is globbed rather than named. It is <publisher>.<id>-<version>.vsix and any
  # of the three can have moved on since the build; an unmatched glob stays literal and
  # falls out at the -f test.
  #
  # `found`, not `removed`: keeping every file is an answer, and reporting "nothing found"
  # after listing four of them was simply untrue.
  local found=0 f
  for f in extension/deploy.local.json extension/vss-extension.local.json \
           extension/dist/agent-config.json extension/dist-vsix/*.vsix; do
    [ -f "${f}" ] || continue
    found=1
    if confirm "Delete ${f}"; then
      rm -f "${f}"
      ok "deleted ${f}"
    else
      skip "kept ${f}"
    fi
  done
  [ "${found}" -eq 1 ] || skip "no generated extension configuration or package found"
  say "the installed .vsix still holds the old values -- uninstall the extension from the"
  say "Azure DevOps organization, or repackage and reinstall"
}

# --------------------------------------------------------------------------------------
# 8 -- Entra app registration
# --------------------------------------------------------------------------------------
# Deleting the application deletes its service principal and, with it, the
# oauth2PermissionGrant. There is nothing separate to clean up.
#
# Two conditions, both required, and neither is a name lookup. Display names are not
# unique in a tenant, so `az ad app list --display-name ticket-to-pr-hub` can return an
# application this repository has never touched -- and an unrecoverable `az ad app delete`
# is not a thing to aim at a guess. Every other destructive step here is gated on a
# T2PR_SETUP_CREATED_* marker; this one is too.
undo8_entra_app() {
  banner "8  Entra app registration"
  local app_id
  app_id="$(env_get T2PR_ENTRA_CLIENT_ID)"
  if [ "$(env_get T2PR_SETUP_CREATED_ENTRA_APP)" != "true" ]; then
    skip "app registration ${app_id:-(none recorded)} was not created by setup.sh -- left alone"
    return 0
  fi
  if [ -z "${app_id}" ] || [ "${app_id}" = "None" ]; then
    warn "T2PR_SETUP_CREATED_ENTRA_APP is set but T2PR_ENTRA_CLIENT_ID is empty."
    warn "Not guessing by display name. Find the app in Entra ID -> App registrations"
    warn "and delete it there, or re-record it: azd env set T2PR_ENTRA_CLIENT_ID <appId>"
    return 0
  fi

  say "app ${app_id} (created by setup.sh)"
  say "deleting it also deletes its service principal and the admin consent grant"
  if ! confirm "Delete the app registration"; then skip "kept ${app_id}"; return 0; fi
  if az ad app delete --id "${app_id}" >/dev/null; then
    ok "deleted app registration ${app_id}"
  else
    warn "could not delete ${app_id} -- you may lack Application Administrator"
  fi
  azd env set T2PR_ENTRA_CLIENT_ID "" >/dev/null 2>&1 || true
  azd_values_refresh
}

# --------------------------------------------------------------------------------------
# 7 -- the ${{connections...}} placeholder
# --------------------------------------------------------------------------------------
undo7_placeholder() {
  banner "7  T2PR_ADO_PAT placeholder"
  if [ -z "$(env_get T2PR_ADO_PAT)" ]; then skip "not set"; return 0; fi
  agent_still_live_warning "clearing the placeholder"
  if ! confirm "Clear the T2PR_ADO_PAT azd variable"; then skip "kept"; return 0; fi
  azd env set T2PR_ADO_PAT "" >/dev/null
  azd_values_refresh
  ok "cleared T2PR_ADO_PAT (it held a placeholder, never a token)"
}

# --------------------------------------------------------------------------------------
# 6 -- the PAT and its Foundry connection
# --------------------------------------------------------------------------------------
undo6_pat() {
  banner "6  Azure DevOps PAT"

  agent_still_live_warning "deleting the connection"

  # Marker-gated, like the Entra app and the two AcrPull grants. setup.sh will happily
  # adopt a connection named 'ado-secrets' that it did not create -- answer "no" to
  # replacing the token and it skips the step entirely -- and a Foundry project can be
  # shared by a second deployment or by a hand-made connection. This holds the PAT, so
  # deleting one this script did not create is the agent_still_live_warning failure in
  # its worst form: the surviving agent fails every run with ado_forbidden and nothing
  # says why.
  if [ "$(env_get T2PR_SETUP_CREATED_ADO_CONNECTION)" != "true" ]; then
    skip "connection ${ADO_CONNECTION_NAME} was not created by setup.sh -- left alone"
  elif azd ai connection show "${ADO_CONNECTION_NAME}" >/dev/null 2>&1; then
    if confirm "Delete the Foundry connection '${ADO_CONNECTION_NAME}'"; then
      if azd ai connection delete "${ADO_CONNECTION_NAME}" --force >/dev/null; then
        ok "deleted connection ${ADO_CONNECTION_NAME}"
      else
        warn "could not delete the connection -- delete it in ai.azure.com"
      fi
    else
      skip "kept connection ${ADO_CONNECTION_NAME}"
    fi
  else
    skip "no connection named ${ADO_CONNECTION_NAME}"
  fi

  # Deleting the connection does not revoke the token. Revoke it too, if setup minted it.
  local auth_id org token
  auth_id="$(env_get T2PR_SETUP_PAT_AUTHORIZATION_ID)"
  org="$(env_get T2PR_SETUP_PAT_ORG)"
  if [ -z "${auth_id}" ] || [ -z "${org}" ]; then
    warn "no minted-token record found. If you pasted a PAT in, revoke it yourself:"
    warn "Azure DevOps -> User settings -> Personal access tokens -> Revoke"
    return 0
  fi
  if ! confirm "Revoke the PAT (authorizationId ${auth_id}) in organization ${org}"; then
    skip "kept the PAT -- remember it stays valid until it expires"
    return 0
  fi
  token="$(az account get-access-token --resource "${ADO_RESOURCE_ID}" --query accessToken -o tsv 2>/dev/null || true)"
  [ -n "${token}" ] || { warn "could not get an Azure DevOps token; revoke the PAT in the UI"; return 0; }
  # Token on stdin as a curl config file (-K -), not -H on the command line: argv is
  # world-readable through /proc/*/cmdline, and this is a bearer token for Azure DevOps.
  if printf 'header = "Authorization: Bearer %s"\n' "${token}" \
      | curl -K - -sS -f -X DELETE \
          "https://vssps.dev.azure.com/${org}/_apis/tokens/pats?authorizationId=${auth_id}&api-version=7.1-preview.1" \
          >/dev/null; then
    ok "revoked the PAT"
  else
    warn "revoke failed; do it in the Azure DevOps UI"
  fi
  azd env set T2PR_SETUP_PAT_AUTHORIZATION_ID "" >/dev/null 2>&1 || true
  azd_values_refresh
}

# --------------------------------------------------------------------------------------
# 5 -- the agent version and its model role
# --------------------------------------------------------------------------------------
undo5_agent() {
  banner "5  Hosted agent and its model role"

  local principal scope
  principal="$(env_get T2PR_SETUP_AGENT_PRINCIPAL_ID)"
  [ -n "${principal}" ] || principal="$(azd ai agent show --output json 2>/dev/null | jget instance_identity.principal_id || true)"
  scope="$(env_get T2PR_SETUP_FOUNDRY_ACCOUNT_SCOPE)"

  if [ -n "${principal}" ] && [ -n "${scope}" ]; then
    # Remove whichever of the three you ended up granting.
    local role
    for role in "Cognitive Services User" "Cognitive Services OpenAI User" "Foundry User"; do
      role_unassign "${principal}" "${role}" "${scope}"
    done
  else
    skip "no recorded agent identity or account scope"
  fi

  if azd ai agent show >/dev/null 2>&1; then
    if confirm "Delete the hosted agent"; then
      if azd ai agent delete --force >/dev/null; then
        ok "deleted the hosted agent"
      else
        warn "azd ai agent delete failed -- delete it in ai.azure.com"
      fi
    else
      skip "kept the hosted agent"
    fi
  else
    skip "no hosted agent deployed"
  fi
}

# --------------------------------------------------------------------------------------
# 4 -- container registry
# --------------------------------------------------------------------------------------
undo4_registry() {
  banner "4  Container registry"

  local acr_id endpoint account rg project_arm_id account_principal project_principal
  acr_id="$(env_get AZURE_CONTAINER_REGISTRY_RESOURCE_ID)"
  if [ -z "${acr_id}" ]; then skip "no registry recorded"; return 0; fi

  endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"
  account_principal="$(az cognitiveservices account show -n "${account}" -g "${rg}" --query identity.principalId -o tsv 2>/dev/null || true)"
  project_arm_id="$(env_get AZURE_AI_PROJECT_ID)"
  project_principal="$(az resource show --ids "${project_arm_id}" --api-version 2025-06-01 --query identity.principalId -o tsv 2>/dev/null || true)"

  # Only the grants setup.sh recorded creating. `role_assign` skips silently when an
  # assignment is already there, and a registry that predates setup.sh usually already
  # carries AcrPull for these principals because something else deploys from it. Removing
  # that breaks the other deployment -- on a registry this script then correctly refuses to
  # delete, because T2PR_SETUP_CREATED_ACR is not true. The two markers are the difference.
  if [ "$(env_get T2PR_SETUP_GRANTED_ACRPULL_ACCOUNT)" = "true" ] && [ -n "${account_principal}" ]; then
    role_unassign "${account_principal}" AcrPull "${acr_id}"
  else
    skip "AcrPull for the Foundry account identity was not granted by setup.sh -- left alone"
  fi
  if [ "$(env_get T2PR_SETUP_GRANTED_ACRPULL_PROJECT)" = "true" ] && [ -n "${project_principal}" ]; then
    role_unassign "${project_principal}" AcrPull "${acr_id}"
  else
    skip "AcrPull for the Foundry project identity was not granted by setup.sh -- left alone"
  fi

  if [ "$(env_get T2PR_SETUP_CREATED_ACR)" = "true" ]; then
    say "registry ${acr_id##*/} was created by setup.sh"
    if confirm "Delete the container registry ${acr_id##*/} and every image in it"; then
      if az acr delete --ids "${acr_id}" --yes >/dev/null; then
        ok "deleted ${acr_id##*/}"
      else
        warn "could not delete the registry"
      fi
    else
      skip "kept the registry"
    fi
  else
    skip "registry ${acr_id##*/} predates setup.sh -- left alone"
  fi
}

# --------------------------------------------------------------------------------------
# 3 -- model deployment and the Foundry project
# --------------------------------------------------------------------------------------
undo3_project() {
  banner "3  Model deployment and Foundry project"

  local endpoint account rg deployment
  endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
  deployment="$(env_get T2PR_MODEL_DEPLOYMENT)"
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"

  if [ "$(env_get T2PR_SETUP_CREATED_MODEL_DEPLOYMENT)" = "true" ] && [ -n "${rg}" ]; then
    if confirm "Delete model deployment '${deployment}' on account '${account}'"; then
      if az cognitiveservices account deployment delete -n "${account}" -g "${rg}" \
           --deployment-name "${deployment}" >/dev/null; then
        ok "deleted deployment ${deployment}"
      else
        warn "could not delete ${deployment}"
      fi
    else
      skip "kept deployment ${deployment}"
    fi
  else
    skip "model deployment ${deployment} predates setup.sh -- left alone"
  fi

  if [ "$(env_get T2PR_SETUP_CREATED_PROJECT)" = "true" ]; then
    say "the Foundry account, project and resource group were created by setup.sh"
    if confirm "Run 'azd down' and delete the whole resource group"; then
      azd down --force --purge || warn "azd down failed -- delete the resource group in the portal"
      ok "azd down complete"
    else
      skip "kept the Foundry project"
    fi
  else
    say "the Foundry project was pre-existing -- not deleting it"
    if confirm "Unbind it (azd ai project unset) and re-comment azure.yaml's endpoint line"; then
      azd ai project unset >/dev/null 2>&1 || true
      python3 - "${AZURE_YAML}" <<'PY'
import re, sys
path = sys.argv[1]
lines = open(path).read().splitlines(keepends=True)
out = []
for line in lines:
    if re.match(r'^\s*#?endpoint: https://', line):
        indent = re.match(r'^(\s*)', line).group(1)
        out.append(f"{indent}#endpoint: https://example.services.ai.azure.com/api/projects/example\n")
    else:
        out.append(line)
open(path, "w").writelines(out)
PY
      ok "unbound the project and reset azure.yaml to the create path"
    else
      skip "left the binding in place"
    fi
  fi
}

# --------------------------------------------------------------------------------------
# 2 -- the azd environment
# --------------------------------------------------------------------------------------
undo2_environment() {
  banner "2  azd environment"
  local name; name="$(env_get AZURE_ENV_NAME)"
  [ -n "${name}" ] || { skip "no environment selected"; return 0; }
  say "environment '${name}' lives in .azure/${name}/ and is gitignored"
  if confirm "Delete .azure/${name}/"; then
    rm -rf ".azure/${name}"
    ok "deleted .azure/${name}"
  else
    skip "kept .azure/${name}"
  fi
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes) ASSUME_YES=1; shift ;;
      # 2,31: the header block above, up to and including the -h|--help line. Adding a
      # marker to the list above moves that line, so this range moves with it.
      -h|--help) sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown option '$1' -- try --help" ;;
    esac
  done

  printf '%sticket-to-pr-agent teardown%s  --  repo %s\n' "${C_BOLD}" "${C_OFF}" "${REPO_ROOT}"
  # Only when it is true. Under --yes there is no prompt to answer, and printing this
  # line immediately before a run that can reach `azd down --force --purge` would be a
  # reassurance the script is about to disprove.
  if [ "${ASSUME_YES}" -eq 0 ]; then
    say "nothing is deleted without a y at the prompt"
  else
    warn "--yes: every prompt below answers itself. Nothing will ask before deleting."
  fi
  azd_values_refresh

  # Not strictly reverse order, and deliberately so. The agent version is offered for
  # deletion BEFORE the PAT connection it depends on and before the placeholder variable
  # that names it. In plain reverse order, answering "no" to "delete the hosted agent"
  # after already saying yes to deleting the connection left a live agent whose
  # ${{connections...}} no longer resolved -- broken, running, and billing.
  # Each banner still carries the setup step it reverses.
  undo9_extension_config
  undo8_entra_app
  undo5_agent
  undo6_pat
  undo7_placeholder
  undo4_registry
  undo3_project
  undo2_environment

  printf '\n%sTeardown complete.%s\n' "${C_GREEN}${C_BOLD}" "${C_OFF}"
  say "Azure DevOps is separate: if you added the agent to the organization by hand,"
  say "remove it under Organization settings -> Users."
}

main "$@"
