#!/usr/bin/env bash
#
# ticket-to-pr-agent -- one-command deploy.
#
#   ./scripts/setup.sh
#
# `azd up` on its own does not produce a working agent. Nine things sit outside it, and
# every one of them was found by a deploy that failed. This script does all nine, in
# order, and is safe to re-run: each step checks the world before it changes it, and a
# step whose work is already done says so and moves on.
#
#   1  preflight: tools, versions, resource providers, logins
#   2  azd environment + the three deploy-time parameters
#   3  Foundry project: create a new one, or connect to an existing one
#   4  container registry, and AcrPull for BOTH identities that pull from it
#   5  the Azure DevOps PAT, stored as a Foundry connection
#   6  the ${{connections...}} indirection that azure.yaml cannot express directly
#   7  first deploy, then a model role for the agent's own instance identity
#   8  the Entra SPA app registration the Azure DevOps extension signs in with
#   9  extension config, the packaged .vsix, and a teardown summary
#
# THE PAT COMES BEFORE THE DEPLOY, and that ordering is load-bearing. A hosted agent's
# environment variables are baked into the agent VERSION at deploy time -- `azd ai agent
# show` returns them under definition.environment_variables. config.py refuses to start
# without T2PR_ADO_PAT, so a version deployed before step 5 and step 6 have run never
# passes its readiness probe and never reaches `active`. Steps 5 and 6 are cheap, local
# and reversible; step 7 is the expensive one, so it runs last and last is also correct.
# Step 7 re-deploys by itself when step 6 recorded the placeholder after the last deploy,
# so a run interrupted between them heals on the next invocation instead of wedging.
#
# Options:
#   --from N     start at step N (1-9) and run to the end
#   --only N     run just step N -- steps 2..9 assume 1..N-1 already ran
#   --list       print the steps and exit
#   -h|--help    this text
#
# Secrets, stated plainly rather than promised away:
#
#   * No PAT or access token is ever printed, logged, or written into a tracked file.
#   * Every `curl` here takes its Authorization header on stdin (`curl -K -`), so no
#     bearer token appears in argv.
#   * ONE exception, step 5: `azd ai connection create --custom-key "pat=..."` puts the
#     PAT on a command line, and that CLI offers no stdin form. For as long as that
#     process lives the token is readable by anyone who can read /proc/*/cmdline on this
#     machine -- in practice, any other process of your uid, and root. Run setup on a
#     machine you trust and do not run it on a shared host. Step 5 says so again before
#     it happens. The value itself lands in the project's Key Vault, not in .azure/.
#
# Step 3 DOES rewrite the tracked `azure.yaml` -- see the note on yaml_set_endpoint.
#
# `scripts/teardown.sh` reverses what this script created.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Azure DevOps' fixed Entra resource id. The only ADO scope this script ever asks for.
ADO_RESOURCE_ID="499b84ac-1321-427f-aa17-267ca6975798"
# Azure Machine Learning Services -- the resource app whose user_impersonation scope the
# hub page requests. See step 8.
AML_APP_ID="18a66f5f-dbdf-4c17-9dd7-1634712a9cbe"
AML_USER_IMPERSONATION_SCOPE_ID="1a7925b5-f871-417a-9b8b-303f9f29fa10"
# The Azure DevOps nested-app-authentication broker redirect. Not a URL you choose.
NAA_REDIRECT_URI="https://dev.azure.com/_public/_MsalPopup"
MIN_AZD_VERSION="1.27.1"
ENTRA_APP_DISPLAY_NAME="ticket-to-pr-hub"
ADO_CONNECTION_NAME="ado-secrets"

# --------------------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------------------
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
else
  C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_OFF=''
fi

banner() { printf '\n%s=== %s ===%s\n' "${C_BOLD}" "$*" "${C_OFF}"; }
say()    { printf '  %s\n' "$*"; }
ok()     { printf '  %s+%s %s\n' "${C_GREEN}" "${C_OFF}" "$*"; }
skip()   { printf '  %s=%s %s %s(already done)%s\n' "${C_DIM}" "${C_OFF}" "$*" "${C_DIM}" "${C_OFF}"; }
warn()   { printf '  %s!%s %s\n' "${C_YELLOW}" "${C_OFF}" "$*" >&2; }
die()    { printf '\n%sFAILED:%s %s\n' "${C_RED}" "${C_OFF}" "$1" >&2
           shift || true
           for line in "$@"; do printf '         %s\n' "${line}" >&2; done
           exit 1; }

# ask <prompt> <default> -> echoes the answer
ask() {
  local prompt="$1" default="${2:-}" reply=''
  if [ -n "${default}" ]; then
    read -r -p "  ${prompt} [${default}]: " reply <"${PROMPT_SRC}" || true
    printf '%s' "${reply:-${default}}"
  else
    read -r -p "  ${prompt}: " reply <"${PROMPT_SRC}" || true
    printf '%s' "${reply}"
  fi
}

# ask_secret <prompt> -> echoes the answer; never echoed back to the terminal
ask_secret() {
  local prompt="$1" reply=''
  read -r -s -p "  ${prompt}: " reply <"${PROMPT_SRC}" || true
  printf '\n' >&2
  printf '%s' "${reply}"
}

confirm() {
  local reply
  reply="$(ask "$1 (y/N)" "n")"
  case "${reply}" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# Prompts read from the controlling terminal so a piped stdin cannot silently answer
# them. Where there is no terminal (CI, a nested tool) fall back to stdin and say so.
if { : </dev/tty; } 2>/dev/null; then PROMPT_SRC=/dev/tty; else PROMPT_SRC=/dev/stdin; fi

# JSON without jq: python3 is already a dependency of this repo.
# jget <path.expr> reads stdin. Missing keys produce the empty string, not a traceback.
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

# --------------------------------------------------------------------------------------
# azd environment helpers
# --------------------------------------------------------------------------------------
AZD_VALUES_CACHE=''

azd_values_refresh() { AZD_VALUES_CACHE="$(azd env get-values -o json 2>/dev/null || echo '{}')"; }

# env_get KEY -> the value, or the empty string
env_get() {
  [ -n "${AZD_VALUES_CACHE}" ] || azd_values_refresh
  printf '%s' "${AZD_VALUES_CACHE}" | jget "$1"
}

# env_set KEY VALUE -- idempotent; skips the write when the value already matches
env_set() {
  local key="$1" value="$2"
  if [ "$(env_get "${key}")" = "${value}" ]; then
    return 0
  fi
  azd env set "${key}" "${value}" >/dev/null
  azd_values_refresh
}

# --------------------------------------------------------------------------------------
# Azure helpers
# --------------------------------------------------------------------------------------
# Set by role_assign to 1 when it actually created the assignment, 0 when one was already
# there. Callers that record a T2PR_SETUP_GRANTED_* marker read it immediately after the
# call. Without it a pre-existing grant is indistinguishable from one this script made,
# and teardown.sh would offer to remove a role assignment somebody else depends on.
ROLE_ASSIGN_CREATED=0

# role_assign <principal-id> <principal-type> <role> <scope> -- idempotent.
# `az role assignment create` is *not* idempotent in a useful way: a duplicate is a
# non-zero exit with RoleAssignmentExists, which `set -e` would turn into a failed run.
role_assign() {
  local principal="$1" ptype="$2" role="$3" scope="$4" existing
  ROLE_ASSIGN_CREATED=0
  existing="$(az role assignment list --assignee "${principal}" --scope "${scope}" \
                --query "[?roleDefinitionName=='${role}'] | length(@)" -o tsv 2>/dev/null || echo 0)"
  if [ "${existing:-0}" != "0" ]; then
    skip "${role} for ${principal} (already granted, not by this script)"
    return 0
  fi
  az role assignment create \
    --assignee-object-id "${principal}" \
    --assignee-principal-type "${ptype}" \
    --role "${role}" \
    --scope "${scope}" >/dev/null \
    || die "could not assign '${role}' to ${principal}" \
           "You need Owner or User Access Administrator on:" \
           "  ${scope}" \
           "Ask whoever owns the subscription to run:" \
           "  az role assignment create --assignee-object-id ${principal} \\" \
           "    --assignee-principal-type ${ptype} --role '${role}' --scope '${scope}'"
  ROLE_ASSIGN_CREATED=1
  ok "${role} for ${principal}"
}

# project_arm_id_from_endpoint <https://acct.services.ai.azure.com/api/projects/name>
# Resolves the ARM id azd needs in AZURE_AI_PROJECT_ID. Fails loudly rather than guessing.
project_arm_id_from_endpoint() {
  local endpoint="$1" account project rg sub
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  project="$(printf '%s' "${endpoint}" | sed -E 's#.*/projects/([^/?]+).*#\1#')"
  [ -n "${account}" ] && [ "${account}" != "${endpoint}" ] \
    || die "could not read the account name out of '${endpoint}'" \
           "Expected https://<account>.services.ai.azure.com/api/projects/<project>"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"
  [ -n "${rg}" ] && [ "${rg}" != "null" ] \
    || die "no Foundry account named '${account}' in the current subscription" \
           "Check 'az account show' is the subscription that owns the project."
  sub="$(az account show --query id -o tsv)"
  printf '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.CognitiveServices/accounts/%s/projects/%s' \
    "${sub}" "${rg}" "${account}" "${project}"
}

version_ge() {
  # version_ge A B -> true when A >= B, comparing dotted numeric components
  printf '%s\n%s\n' "$2" "$1" | sort -V -C
}

# --------------------------------------------------------------------------------------
# Step 1 -- preflight
# --------------------------------------------------------------------------------------
#
# The resource providers. Nothing else in this script registers anything, and a
# subscription that has never run a Foundry or azd workload commonly has
# Microsoft.CognitiveServices unregistered. `azd provision` then fails with
#   MissingSubscriptionRegistration: The subscription is not registered to use namespace
#   'Microsoft.CognitiveServices'
# which step 3's die text used to misattribute to a missing Contributor role or a region
# where hosted agents are unavailable. Registration is idempotent, subscription-wide and
# takes a minute or two; checking is a single read.
REQUIRED_PROVIDERS=(
  Microsoft.CognitiveServices      # the Foundry (AI Services) account, projects, models
  Microsoft.ContainerRegistry      # the registry azd builds the agent image into
  Microsoft.OperationalInsights    # the Log Analytics workspace azd provisions alongside
  Microsoft.MachineLearningServices # the Foundry data plane the extension signs in to
)

ensure_providers() {
  local ns state pending=()
  for ns in "${REQUIRED_PROVIDERS[@]}"; do
    state="$(az provider show -n "${ns}" --query registrationState -o tsv 2>/dev/null || true)"
    if [ "${state}" = "Registered" ]; then
      skip "resource provider ${ns}"
    else
      warn "resource provider ${ns} is ${state:-unknown}, not Registered"
      pending+=("${ns}")
    fi
  done
  [ "${#pending[@]}" -gt 0 ] || return 0

  say ""
  say "Registering a resource provider is subscription-wide, idempotent and takes a"
  say "minute or two. Without it 'azd provision' fails with MissingSubscriptionRegistration."
  if ! confirm "Register ${pending[*]} now"; then
    die "these resource providers are not registered:  ${pending[*]}" \
        "Register them yourself, or ask whoever owns the subscription to:" \
        "$(for ns in "${pending[@]}"; do printf '  az provider register -n %s --wait\n' "${ns}"; done)"
  fi
  for ns in "${pending[@]}"; do
    say "registering ${ns}..."
    az provider register -n "${ns}" --wait >/dev/null 2>&1 \
      || die "could not register ${ns}" \
             "You need Contributor or Owner on the subscription. Ask someone who has it:" \
             "  az provider register -n ${ns} --wait"
    ok "resource provider ${ns}"
  done
}

step1_preflight() {
  banner "1/9  Preflight"

  # curl is not optional: step 5 mints the PAT with it and teardown.sh revokes with it.
  # It is absent from python:*-slim and from plenty of minimal CI images.
  local missing=0
  for tool in az azd git python3 curl; do
    if have "${tool}"; then
      ok "${tool}"
    else
      warn "${tool} not found"
      missing=1
    fi
  done
  [ "${missing}" -eq 0 ] || die "install the tools listed above and re-run" \
    "  az     https://learn.microsoft.com/cli/azure/install-azure-cli" \
    "  azd    https://aka.ms/install-azd" \
    "  curl   used to mint and to revoke the Azure DevOps PAT"

  # node and npm are a WARNING, not a requirement. Nothing in steps 1..8 touches them:
  # they build the Azure DevOps extension in step 9, which is the one part of this script
  # that is not the agent. By the time step 9 runs the agent is deployed and answering,
  # and a .vsix can be built on any machine that can read this azd environment. So a host
  # without Node gets a working agent and four commands to run elsewhere, not a failure.
  if have node && have npm; then
    ok "node $(node --version 2>/dev/null) / npm $(npm --version 2>/dev/null)"
  else
    warn "node/npm not found -- step 9 will skip building the extension .vsix"
    say "    and print the commands to run on a machine that has Node LTS instead."
    say "    Nothing else in this script needs them.  https://nodejs.org"
  fi

  # Docker is optional: azd builds the image remotely inside ACR by default. It is only
  # needed if you set remoteBuild:false on the agent service, or want to build locally.
  if have docker && docker info >/dev/null 2>&1; then
    ok "docker (running)"
  elif have docker; then
    warn "docker is installed but not running -- fine unless you build locally"
  else
    warn "docker not found -- fine unless you build locally (azd builds inside ACR)"
  fi

  local azd_version
  azd_version="$(azd version 2>/dev/null | sed -E 's/^azd version ([0-9.]+).*/\1/' | head -1)"
  if [ -z "${azd_version}" ]; then
    die "could not read 'azd version'"
  fi
  version_ge "${azd_version}" "${MIN_AZD_VERSION}" \
    || die "azd ${azd_version} is too old; ${MIN_AZD_VERSION} or later is required" \
           "  brew upgrade azd            (macOS)" \
           "  curl -fsSL https://aka.ms/install-azd.sh | bash   (Linux)" \
           "  winget upgrade microsoft.azd                      (Windows)"
  ok "azd ${azd_version}"

  # microsoft.foundry is a meta-package: it pulls in azure.ai.projects, azure.ai.agents
  # and azure.ai.connections, all three of which this deployment uses.
  if azd extension list --installed 2>/dev/null | grep -q '^microsoft.foundry'; then
    skip "azd extension microsoft.foundry"
  else
    say "installing the microsoft.foundry azd extension..."
    azd extension install microsoft.foundry >/dev/null \
      || die "azd extension install microsoft.foundry failed" \
             "Run it by hand to see the error."
    ok "azd extension microsoft.foundry"
  fi

  if az account show >/dev/null 2>&1; then
    ok "az logged in as $(az account show --query user.name -o tsv 2>/dev/null)"
    say "    subscription: $(az account show --query '[name,id]' -o tsv 2>/dev/null | tr '\n' ' ')"
  else
    say "running 'az login'..."
    az login >/dev/null || die "az login failed"
    ok "az logged in"
  fi

  # This is the difference between one login and two. With auth.useAzCliAuth set, azd
  # reuses the az CLI's token cache instead of running its own interactive browser login.
  if [ "$(azd config get auth.useAzCliAuth 2>/dev/null | tr -d '"')" = "true" ]; then
    skip "azd auth.useAzCliAuth"
  else
    azd config set auth.useAzCliAuth true >/dev/null
    ok "azd auth.useAzCliAuth true (azd now reuses your az login)"
  fi

  if azd auth login --check-status >/dev/null 2>&1; then
    ok "azd authenticated"
  else
    say "running 'azd auth login'..."
    azd auth login >/dev/null || die "azd auth login failed"
    ok "azd authenticated"
  fi

  # After the az login, because it reads the subscription.
  ensure_providers
}

# --------------------------------------------------------------------------------------
# Step 2 -- azd environment and the three deploy-time parameters
# --------------------------------------------------------------------------------------
step2_environment() {
  banner "2/9  azd environment and parameters"

  local current
  current="$(azd env list -o json 2>/dev/null | python3 -c '
import json, sys
try:
    for e in json.load(sys.stdin):
        if e.get("IsDefault") or e.get("isDefault"):
            print(e.get("Name") or e.get("name")); break
except Exception:
    pass
' || true)"

  if [ -n "${current}" ]; then
    say "current environment: ${current}"
    if confirm "Use it"; then
      azd env select "${current}" >/dev/null
    else
      current=''
    fi
  fi

  if [ -z "${current}" ]; then
    current="$(ask "Environment name (e.g. dev, or t2pr-westus3)" "dev")"
    [ -n "${current}" ] || die "an environment name is required"
    # Exact match on the Name field, not `grep "\"${current}\""` over the whole JSON:
    # that is a substring test and environment `dev` matches an existing `dev-2`, so
    # `azd env select dev` runs against an environment that does not exist.
    if azd env list -o json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
try:
    envs = json.load(sys.stdin)
except Exception:
    envs = []
sys.exit(0 if any((e.get("Name") or e.get("name")) == want for e in envs) else 1)
' "${current}"; then
      azd env select "${current}" >/dev/null
    else
      azd env new "${current}" >/dev/null || die "azd env new ${current} failed"
    fi
  fi
  ok "environment ${current}"
  azd_values_refresh

  local sub
  sub="$(env_get AZURE_SUBSCRIPTION_ID)"
  if [ -z "${sub}" ]; then
    sub="$(az account show --query id -o tsv)"
    env_set AZURE_SUBSCRIPTION_ID "${sub}"
  fi
  ok "subscription ${sub}"

  local location
  location="$(env_get AZURE_LOCATION)"
  if [ -z "${location}" ]; then
    say "Pick a region where hosted agents AND your model are available."
    say "swedencentral, eastus2 and westus3 are safe choices."
    location="$(ask "Azure location" "swedencentral")"
    env_set AZURE_LOCATION "${location}"
  fi
  ok "location ${location}"

  # The three deploy-time parameters (docs/configuration.md section 1). These are the
  # ${VAR}s the agent service's env: map in azure.yaml resolves.
  local harness model orgs
  say ""
  say "T2PR_HARNESS: 'claude-code' or 'codex'."
  say "  codex currently 401s against Foundry -- Microsoft's own docs say Entra ID"
  say "  support is not available for Codex, and this project uses no API keys."
  harness="$(ask "T2PR_HARNESS" "$(env_get T2PR_HARNESS || true)")"
  [ -n "${harness}" ] || harness="claude-code"
  case "${harness}" in
    claude-code|codex) ;;
    *) die "T2PR_HARNESS must be 'claude-code' or 'codex', not '${harness}'" ;;
  esac
  env_set T2PR_HARNESS "${harness}"

  say ""
  say "T2PR_MODEL_DEPLOYMENT: the *deployment* name on the Foundry account -- the name"
  say "  you chose, not the catalog model id. Step 3 creates it if it is missing."
  local model_default
  model_default="$(env_get T2PR_MODEL_DEPLOYMENT)"
  if [ -z "${model_default}" ]; then
    if [ "${harness}" = "claude-code" ]; then model_default="claude-opus-5"; else model_default="gpt-6"; fi
  fi
  model="$(ask "T2PR_MODEL_DEPLOYMENT" "${model_default}")"
  env_set T2PR_MODEL_DEPLOYMENT "${model}"

  say ""
  say "T2PR_ALLOWED_ORGS: comma-separated Azure DevOps organizations the agent may act"
  say "  on. An empty allowlist rejects every request and the container refuses to start."
  orgs="$(ask "T2PR_ALLOWED_ORGS" "$(env_get T2PR_ALLOWED_ORGS || true)")"
  [ -n "${orgs}" ] || die "T2PR_ALLOWED_ORGS cannot be empty" \
    "The agent validates every request's 'organization' against it and refuses to" \
    "start without at least one entry."
  env_set T2PR_ALLOWED_ORGS "${orgs}"

  ok "T2PR_HARNESS=${harness}  T2PR_MODEL_DEPLOYMENT=${model}  T2PR_ALLOWED_ORGS=${orgs}"
}

# --------------------------------------------------------------------------------------
# Step 3 -- Foundry project: create, or connect to an existing one
# --------------------------------------------------------------------------------------
#
# azure.yaml expresses both, and the switch is one line: the `endpoint:` under the
# ai-project service, which this function writes or comments out. It has to be a literal
# -- azd does NOT substitute ${VAR} inside an azure.ai.project service. Verified with
# `azd provision --preview` on 2026-09-10: `name: ${T2PR_MODEL_DEPLOYMENT}` under
# `deployments:` is planned as a model deployment literally called
# "${T2PR_MODEL_DEPLOYMENT}". Hence the marker-line rewrite below rather than a variable.
#
# THIS WRITES A TRACKED FILE, and there is no way around it. azure.yaml is the only path
# azd reads; there is no azure.local.yaml, and the azd environment cannot supply the value
# because of the non-substitution above. So on the connect path your Foundry account and
# project names end up in `git status` as a modification to azure.yaml. That is
# tenant-specific: committing and pushing it publishes your account name. Step 3 says so
# out loud, and step 9 repeats it with the command to revert.
AZURE_YAML="azure.yaml"

yaml_set_endpoint() {
  python3 - "$@" <<'PY'
import re, sys
path, endpoint = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True)
out, seen = [], False
for line in lines:
    if re.match(r'^\s*#?endpoint: https://', line):
        indent = re.match(r'^(\s*)', line).group(1)
        out.append(f"{indent}endpoint: {endpoint}\n" if endpoint
                   else f"{indent}#endpoint: https://example.services.ai.azure.com/api/projects/example\n")
        seen = True
    else:
        out.append(line)
if not seen:
    raise SystemExit("azure.yaml has no endpoint marker line under the ai-project service")
open(path, "w").writelines(out)
PY
}

step3_project() {
  banner "3/9  Foundry project"

  local endpoint choice
  endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
  if [ -z "${endpoint}" ]; then
    endpoint="$(azd ai project show -o json 2>/dev/null | jget endpoint || true)"
  fi

  if [ -n "${endpoint}" ]; then
    say "already bound to: ${endpoint}"
    if confirm "Keep it"; then
      choice="connect"
    else
      endpoint=''
    fi
  fi

  if [ -z "${endpoint}" ]; then
    say ""
    say "  [1] Create a new Foundry account + project (azd provisions everything)"
    say "  [2] Connect to a Foundry project that already exists"
    case "$(ask "Choose" "1")" in
      2) choice="connect" ;;
      *) choice="create" ;;
    esac
  fi

  if [ "${choice}" = "connect" ]; then
    if [ -z "${endpoint}" ]; then
      say "The project endpoint is on the project's overview page in ai.azure.com."
      endpoint="$(ask "Project endpoint (https://<account>.services.ai.azure.com/api/projects/<project>)" "")"
    fi
    [ -n "${endpoint}" ] || die "a project endpoint is required to connect"

    yaml_set_endpoint "${AZURE_YAML}" "${endpoint}"
    ok "azure.yaml ai-project endpoint -> ${endpoint}"
    warn "that edited ${AZURE_YAML}, which is a TRACKED file."
    say "azd reads the endpoint only from there and will not take it from a variable, so"
    say "your Foundry account and project names are now a local modification to it. Do not"
    say "commit it to a public fork. Revert with:  git checkout -- ${AZURE_YAML}"

    azd ai project set "${endpoint}" >/dev/null \
      || die "azd ai project set '${endpoint}' failed" \
             "Check the endpoint is reachable and you have a Foundry role on the project."
    env_set FOUNDRY_PROJECT_ENDPOINT "${endpoint}"

    # Without this azd refuses to provision:
    #   "AZURE_AI_PROJECT_ID does not identify the project configured by the azure.yaml
    #    endpoint" / "re-run `azd ai agent init` against the configured existing project".
    # azd will not derive it from the endpoint, so we do.
    local arm_id
    arm_id="$(project_arm_id_from_endpoint "${endpoint}")"
    az resource show --ids "${arm_id}" --api-version 2025-06-01 >/dev/null 2>&1 \
      || die "the project ARM id derived from the endpoint does not exist:" "  ${arm_id}"
    env_set AZURE_AI_PROJECT_ID "${arm_id}"
    ok "AZURE_AI_PROJECT_ID ${arm_id}"
    env_set T2PR_SETUP_CREATED_PROJECT "false"
  else
    yaml_set_endpoint "${AZURE_YAML}" ""
    ok "azure.yaml ai-project endpoint commented out (create path)"
    say "running 'azd provision' -- this creates the resource group, the Foundry"
    say "account, the project and the container registry. 5-15 minutes."
    azd provision || die "azd provision failed" \
      "Read the error above. The usual causes are a missing Contributor role on the" \
      "subscription, a region where hosted agents are not available yet, or a quota" \
      "the subscription does not have. Step 1 already checked resource-provider" \
      "registration, so MissingSubscriptionRegistration is not one of them."
    env_set T2PR_SETUP_CREATED_PROJECT "true"
    azd_values_refresh
    endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
    # azd writes this back after a successful provision. When it does not -- a partial
    # provision, or an extension version that names the output differently -- everything
    # downstream derives the account name from an empty string and step 3's own
    # ensure_model_deployment dies with "could not find the resource group of Foundry
    # account ''", which says nothing about the real cause. Say the real cause here.
    [ -n "${endpoint}" ] || die "azd provision succeeded but wrote no FOUNDRY_PROJECT_ENDPOINT" \
      "Nothing downstream can find the Foundry account without it. Check:" \
      "  azd env get-values | grep -i foundry" \
      "  azd ai project show" \
      "If the project exists, record it and re-run from step 3:" \
      "  azd env set FOUNDRY_PROJECT_ENDPOINT https://<account>.services.ai.azure.com/api/projects/<project>"
    ok "project ${endpoint}"
  fi

  ensure_model_deployment "${endpoint}"
}

# The model deployment cannot come from azure.yaml when it is an Anthropic model:
#   "InvalidModelProviderData: ModelProviderData is required for Anthropic model
#    deployments but was not provided. Please provide all required fields: industry,
#    organizationName, and countryCode."
# and azure.yaml's `deployments` schema has no field for modelProviderData. `az
# cognitiveservices account deployment create` has no flag for it either, so this is a
# raw ARM PUT. The three fields are what the resource provider uses to auto-sign the
# Azure Marketplace offer on your behalf.
# The three helpers below exist because none of the model answers can be hard-coded on a
# subscription this script has never seen. `claude-opus-5` / version `2` / capacity `40`
# were this deployment's answers, not universal ones.
MODEL_SKU="GlobalStandard"

# model_quota <location> <sku> <model> -- prints "<limit> <consumed>", or nothing when the
# subscription has no counter for that model at all.
#
# `az cognitiveservices usage list` names each counter AIServices.<sku>.<model>, and there
# is a parallel `.Azure` counter carrying the consumption booked against the Azure route.
# Read against a live subscription 2026-09-10:
#   AIServices.GlobalStandard.claude-opus-5         limit 40  current  0
#   AIServices.GlobalStandard.claude-opus-5.Azure   limit 40  current 40
# One deployment of 40 exists, so the honest headroom is the highest limit reported minus
# the highest consumption reported -- reading either counter alone gets it wrong. A model
# the subscription is not entitled to is absent, or present with limit 0 (observed for
# claude-sonnet-5 in the same read).
model_quota() {
  az cognitiveservices usage list -l "$1" -o json 2>/dev/null \
    | python3 -c '
import json, sys
sku, model = sys.argv[1], sys.argv[2]
wanted = {"AIServices.%s.%s" % (sku, model), "AIServices.%s.%s.Azure" % (sku, model)}
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
hits = [r for r in rows if ((r.get("name") or {}).get("value") or "") in wanted]
if hits:
    print(int(max(h.get("limit") or 0 for h in hits)),
          int(max(h.get("currentValue") or 0 for h in hits)))
' "$2" "$3"
}

# model_default_version <account> <rg> <format> <model> -- the catalog's current default
# version for that model on that account, or nothing when the account cannot offer it.
model_default_version() {
  az cognitiveservices account list-models -n "$1" -g "$2" -o json 2>/dev/null \
    | python3 -c '
import json, sys
fmt, name = sys.argv[1], sys.argv[2]
try:
    models = json.load(sys.stdin)
except Exception:
    models = []
cands = [m for m in models if m.get("format") == fmt and m.get("name") == name]
if not cands:
    sys.exit(0)
best = next((m for m in cands if m.get("isDefaultVersion")), None)
print((best or max(cands, key=lambda m: str(m.get("version") or ""))).get("version") or "")
' "$3" "$4"
}

# wait_for_deployment <account> <rg> <deployment> -- 0 Succeeded, 1 Failed, 2 timed out.
# `az rest --method put` returns as soon as ARM accepts the request. An accepted-but-still
# -running deployment looks exactly like a finished one, and the next step then deploys an
# agent pointed at a model that is not there yet.
wait_for_deployment() {
  local account="$1" rg="$2" name="$3" state deadline
  deadline=$(( $(date +%s) + 900 ))
  while :; do
    state="$(az cognitiveservices account deployment show -n "${account}" -g "${rg}" \
               --deployment-name "${name}" --query properties.provisioningState \
               -o tsv 2>/dev/null || true)"
    case "${state}" in
      Succeeded) return 0 ;;
      Failed|Canceled) return 1 ;;
    esac
    [ "$(date +%s)" -lt "${deadline}" ] || return 2
    sleep 10
  done
}

ensure_model_deployment() {
  local endpoint="$1" deployment account rg sub location existing
  deployment="$(env_get T2PR_MODEL_DEPLOYMENT)"
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"
  [ -n "${rg}" ] || die "could not find the resource group of Foundry account '${account}'" \
    "Derived from FOUNDRY_PROJECT_ENDPOINT=${endpoint:-(empty)}" \
    "If that is empty, provision did not write it back -- re-run step 3."
  sub="$(az account show --query id -o tsv)"
  # The account's own region, not AZURE_LOCATION: on the connect path an existing account
  # can live somewhere else entirely, and quota is per region.
  location="$(az cognitiveservices account show -n "${account}" -g "${rg}" --query location -o tsv 2>/dev/null || true)"
  [ -n "${location}" ] || location="$(env_get AZURE_LOCATION)"

  existing="$(az cognitiveservices account deployment show \
                -n "${account}" -g "${rg}" --deployment-name "${deployment}" \
                --query name -o tsv 2>/dev/null || true)"
  if [ -n "${existing}" ]; then
    skip "model deployment ${deployment}"
    env_set T2PR_SETUP_MODEL_DEPLOYMENT_MISSING "false"
    return 0
  fi

  say ""
  say "No deployment named '${deployment}' on account '${account}'."
  if ! confirm "Create it now"; then
    # A marker, not just a warning. The old code printed a warning and let the run
    # continue into a deploy that 404s on every invocation for the life of the agent
    # version. Step 7 refuses to deploy while this is true.
    env_set T2PR_SETUP_MODEL_DEPLOYMENT_MISSING "true"
    warn "skipping -- '${deployment}' does not exist, so step 7 will refuse to deploy"
    say "Create it in ai.azure.com, or re-run:  ./scripts/setup.sh --only 3"
    return 0
  fi

  local fmt model_name model_version capacity org country industry default_version
  if [ "$(env_get T2PR_HARNESS)" = "claude-code" ]; then
    fmt="Anthropic"
    model_name="$(ask "Catalog model name" "claude-opus-5")"
  else
    fmt="OpenAI"
    model_name="$(ask "Catalog model name" "gpt-6-astra")"
  fi

  # What this subscription can actually deploy, asked before the PUT rather than inferred
  # from its error message.
  local quota limit consumed available
  quota="$(model_quota "${location}" "${MODEL_SKU}" "${model_name}")"
  if [ -z "${quota}" ]; then
    warn "no ${MODEL_SKU} quota counter for '${model_name}' in ${location}."
    say "That usually means this subscription is not entitled to the model, or the name"
    say "is not a catalog model id. 'az cognitiveservices usage list -l ${location}' lists"
    say "every counter; 'az cognitiveservices account list-models -n ${account} -g ${rg}'"
    say "lists what this account can deploy."
    confirm "Try the deployment anyway" || die "stopped before creating '${deployment}'." \
      "Pick a model this subscription is entitled to and re-run:  ./scripts/setup.sh --only 3"
    limit=''; available=''
  else
    limit="${quota%% *}"; consumed="${quota##* }"
    available=$(( limit - consumed ))
    ok "quota for ${model_name} in ${location}: limit ${limit}k TPM, ${consumed}k used, ${available}k free"
    if [ "${limit}" -eq 0 ]; then
      die "this subscription has 0k TPM of ${MODEL_SKU} quota for '${model_name}' in ${location}" \
        "The counter exists but the limit is zero, which is what an unentitled model looks" \
        "like. Request quota in the portal (Foundry -> Quotas), pick a different model, or" \
        "pick a different region, then re-run:  ./scripts/setup.sh --only 3"
    fi
  fi

  default_version="$(model_default_version "${account}" "${rg}" "${fmt}" "${model_name}")"
  if [ -z "${default_version}" ]; then
    warn "account '${account}' does not list ${fmt} model '${model_name}' -- the version"
    warn "below is a guess and the PUT may fail with a catalog error."
    if [ "${fmt}" = "Anthropic" ]; then default_version="2"; else default_version="2026-09-03"; fi
  fi
  model_version="$(ask "Model version" "${default_version}")"

  # Default the capacity to what is free rather than to this deployment's 40.
  local capacity_default="40"
  [ "${fmt}" = "Anthropic" ] || capacity_default="50"
  if [ -n "${available}" ] && [ "${available}" -gt 0 ] && [ "${available}" -lt "${capacity_default}" ]; then
    capacity_default="${available}"
  fi
  capacity="$(ask "Capacity (thousands of tokens per minute)" "${capacity_default}")"
  case "${capacity}" in
    ''|*[!0-9]*) die "capacity must be a whole number of thousands of TPM, not '${capacity}'" ;;
  esac
  if [ -n "${available}" ] && [ "${capacity}" -gt "${available}" ]; then
    warn "asking for ${capacity}k but only ${available}k of ${limit}k is free in ${location}."
    say "ARM will reject this with an InsufficientQuota error."
    confirm "Send it anyway" || die "stopped before creating '${deployment}'." \
      "Lower the capacity, free quota by deleting an unused deployment, or request more."
  fi

  if [ "${fmt}" = "Anthropic" ]; then
    say ""
    say "Anthropic deployments need three Marketplace fields so the resource provider"
    say "can auto-sign the offer for you. They are not secret and not validated."
    org="$(ask "Marketplace: organization name" "$(az account show --query name -o tsv)")"
    country="$(ask "Marketplace: ISO 3166 country code" "US")"
    industry="$(ask "Marketplace: industry" "Technology")"
  else
    org=""; country=""; industry=""
  fi

  local body_file
  body_file="$(mktemp "${TMPDIR:-/tmp}/t2pr-model.XXXXXX")"
  python3 -c '
import json, sys
sku, fmt, name, version, capacity, org, country, industry = sys.argv[1:9]
props = {"model": {"format": fmt, "name": name, "version": version}}
if fmt == "Anthropic":
    props["modelProviderData"] = {
        "organizationName": org, "countryCode": country, "industry": industry,
    }
print(json.dumps({"sku": {"name": sku, "capacity": int(capacity)},
                  "properties": props}))
' "${MODEL_SKU}" "${fmt}" "${model_name}" "${model_version}" "${capacity}" "${org}" "${country}" "${industry}" \
    > "${body_file}"

  az rest --method put \
    --url "https://management.azure.com/subscriptions/${sub}/resourceGroups/${rg}/providers/Microsoft.CognitiveServices/accounts/${account}/deployments/${deployment}?api-version=2025-06-01" \
    --headers "Content-Type=application/json" \
    --body "@${body_file}" >/dev/null \
    || { rm -f "${body_file}"
         die "creating model deployment '${deployment}' failed" \
             "If the error mentions quota, the numbers above say what is free; request" \
             "more for ${model_name} in ${location}, or lower the capacity." \
             "If it mentions ModelProviderData, one of the three Marketplace answers was" \
             "empty. If it mentions the Marketplace offer, accept it once in ai.azure.com."; }
  rm -f "${body_file}"

  # ARM accepts this asynchronously, so a 200 is not a finished deployment.
  say "waiting for '${deployment}' to finish provisioning..."
  local dep_state=0
  wait_for_deployment "${account}" "${rg}" "${deployment}" || dep_state=$?
  case "${dep_state}" in
    0) : ;;
    1) die "model deployment '${deployment}' finished in a failed state" \
           "Look at it in ai.azure.com, delete it, and re-run:  ./scripts/setup.sh --only 3" ;;
    *) die "model deployment '${deployment}' was still provisioning after 15 minutes" \
           "Check it with:" \
           "  az cognitiveservices account deployment show -n ${account} -g ${rg} \\" \
           "    --deployment-name ${deployment} --query properties.provisioningState" \
           "then re-run:  ./scripts/setup.sh --from 4" ;;
  esac

  env_set T2PR_SETUP_CREATED_MODEL_DEPLOYMENT "true"
  env_set T2PR_SETUP_MODEL_DEPLOYMENT_MISSING "false"
  ok "model deployment ${deployment} (${fmt} ${model_name} ${model_version}, capacity ${capacity})"
}

# --------------------------------------------------------------------------------------
# Step 4 -- container registry, and AcrPull for BOTH identities
# --------------------------------------------------------------------------------------
#
# azd's own Bicep assigns AcrPull to the *project* managed identity and stops there. The
# image is pulled by the Cognitive Services *account* identity as well, and without a
# grant for it the deploy dies with:
#   [ImageError] Container registry authentication failed
# which names neither the registry nor the identity. That cost two failed deploys.
step4_registry() {
  banner "4/9  Container registry and AcrPull"

  local endpoint account rg sub acr_name acr_id acr_endpoint
  endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
  [ -n "${endpoint}" ] || die "no FOUNDRY_PROJECT_ENDPOINT -- run step 3 first"
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv)"
  sub="$(az account show --query id -o tsv)"

  acr_id="$(env_get AZURE_CONTAINER_REGISTRY_RESOURCE_ID)"
  acr_endpoint="$(env_get AZURE_CONTAINER_REGISTRY_ENDPOINT)"

  if [ -z "${acr_id}" ] && [ -n "${acr_endpoint}" ]; then
    acr_name="${acr_endpoint%%.*}"
    acr_id="$(az acr show -n "${acr_name}" --query id -o tsv 2>/dev/null || true)"
  fi

  # The name this script would create, computed first so it can also be searched for.
  local env_name acr_want
  env_name="$(env_get AZURE_ENV_NAME)"
  acr_want="acr$(printf '%s' "${env_name}" | tr -cd '[:alnum:]' | cut -c1-10)$(printf '%s' "${sub}" | tr -cd '[:alnum:]' | cut -c1-6)"

  # Adopt only a registry that belongs to this deployment: one azd tagged with this
  # environment name, or one named exactly what this script names its own. The old
  # `az acr list -g "${rg}" --query '[0].id'` took whatever registry happened to sort
  # first in the resource group -- on a shared resource group that is somebody else's,
  # and step 4 then grants AcrPull on it and records the grant for teardown to remove.
  if [ -z "${acr_id}" ] && [ -n "${env_name}" ]; then
    acr_id="$(az acr list -g "${rg}" \
                --query "[?tags.\"azd-env-name\"=='${env_name}'].id | [0]" -o tsv 2>/dev/null || true)"
  fi
  if [ -z "${acr_id}" ] || [ "${acr_id}" = "null" ]; then
    acr_id="$(az acr list -g "${rg}" --query "[?name=='${acr_want}'].id | [0]" -o tsv 2>/dev/null || true)"
  fi

  if [ -z "${acr_id}" ] || [ "${acr_id}" = "null" ]; then
    acr_name="${acr_want}"
    say "no registry belonging to this deployment in ${rg}; creating ${acr_name}"
    az acr create -n "${acr_name}" -g "${rg}" --sku Basic --admin-enabled false >/dev/null \
      || die "az acr create failed" "Registry names are globally unique, 5-50 alphanumeric characters."
    acr_id="$(az acr show -n "${acr_name}" --query id -o tsv)"
    env_set T2PR_SETUP_CREATED_ACR "true"
    ok "registry ${acr_name}"
  else
    acr_name="${acr_id##*/}"
    skip "registry ${acr_name}"
  fi

  acr_endpoint="$(az acr show -n "${acr_name}" --query loginServer -o tsv)"
  # Both of these. azd's provisioning writes the endpoint but not the resource id, and
  # `azd provision` then refuses with "reuse-connect requires both container registry
  # endpoint and resource ID". Verified 2026-09-10.
  env_set AZURE_CONTAINER_REGISTRY_ENDPOINT "${acr_endpoint}"
  env_set AZURE_CONTAINER_REGISTRY_RESOURCE_ID "${acr_id}"
  ok "AZURE_CONTAINER_REGISTRY_ENDPOINT=${acr_endpoint}"

  local account_principal project_principal project_arm_id
  account_principal="$(az cognitiveservices account show -n "${account}" -g "${rg}" \
                        --query identity.principalId -o tsv 2>/dev/null || true)"
  project_arm_id="$(env_get AZURE_AI_PROJECT_ID)"
  [ -n "${project_arm_id}" ] || project_arm_id="$(project_arm_id_from_endpoint "${endpoint}")"
  project_principal="$(az resource show --ids "${project_arm_id}" --api-version 2025-06-01 \
                        --query identity.principalId -o tsv 2>/dev/null || true)"

  [ -n "${account_principal}" ] || die "the Foundry account '${account}' has no managed identity" \
    "Enable a system-assigned identity on it: az cognitiveservices account identity assign -n ${account} -g ${rg}"
  [ -n "${project_principal}" ] || die "the Foundry project has no managed identity" \
    "  ${project_arm_id}"

  # BOTH. Granting only the account identity is the failure mode described above.
  #
  # The markers are recorded per identity and only on the branch that actually created the
  # grant. A registry that predates setup.sh very often already carries AcrPull for these
  # principals because something else deploys from it; teardown.sh offering to strip that
  # would break the other deployment, on a registry it correctly refuses to delete.
  role_assign "${account_principal}" ServicePrincipal AcrPull "${acr_id}"
  if [ "${ROLE_ASSIGN_CREATED}" -eq 1 ]; then
    env_set T2PR_SETUP_GRANTED_ACRPULL_ACCOUNT "true"
  fi
  role_assign "${project_principal}" ServicePrincipal AcrPull "${acr_id}"
  if [ "${ROLE_ASSIGN_CREATED}" -eq 1 ]; then
    env_set T2PR_SETUP_GRANTED_ACRPULL_PROJECT "true"
  fi

  # On the connect path step 3 never ran `azd provision` -- the project already existed.
  # Run it now that both registry variables are set, so azd creates the project's
  # container-registry connection. `azd provision --preview` against an already-complete
  # environment plans exactly one resource, that connection, so this is close to a no-op
  # when it has already been done. On the create path step 3 provisioned everything.
  if [ "$(env_get T2PR_SETUP_CREATED_PROJECT)" = "false" ]; then
    say "running 'azd provision' to create the project's registry connection..."
    azd provision || die "azd provision failed" \
      "Run 'azd provision --preview' to see the plan without applying it."
    azd_values_refresh
    ok "project registry connection"
  fi

  say "role assignments can take a minute to propagate before a pull succeeds"
}

# --------------------------------------------------------------------------------------
# Step 5 -- the Azure DevOps PAT
# --------------------------------------------------------------------------------------
step5_pat() {
  banner "5/9  Azure DevOps personal access token"

  local orgs org
  orgs="$(env_get T2PR_ALLOWED_ORGS)"
  org="${orgs%%,*}"
  [ -n "${org}" ] || die "T2PR_ALLOWED_ORGS is empty -- run step 2 first"

  cat <<EOF

  The agent authenticates to Azure DevOps with a PAT, not with its Entra identity.
  Azure DevOps will not entitle a Foundry agent identity (VS403283) and that identity
  cannot federate into one it does accept (AADSTS700231). docs/deploy.md section 3 has
  the evidence: four routes, tried against a live tenant and organization.

  The token needs exactly three scopes, and nothing else:

    Work Items    Read           fetch the work item and its linked items
    Code          Read & Write   clone the base branch, push the new one
    Pull Request  Read & Write   open the pull request and add reviewers

  Scope it to the single organization '${org}'. Not "all accessible organizations".
  Prefer a service account over a person's: it survives them leaving, and its actions
  are auditable as the agent's rather than theirs.

EOF

  local existing
  existing="$(azd ai connection list -o json 2>/dev/null \
              | python3 -c 'import json,sys
try: print("\n".join(c.get("name","") for c in json.load(sys.stdin)))
except Exception: pass' | grep -Fx "${ADO_CONNECTION_NAME}" || true)"
  if [ -n "${existing}" ]; then
    say "a Foundry connection named '${ADO_CONNECTION_NAME}' already exists"
    if ! confirm "Replace the token in it"; then
      skip "PAT connection ${ADO_CONNECTION_NAME}"
      return 0
    fi
  fi

  warn "the next command puts the PAT on a command line."
  say "\`azd ai connection create\` takes the secret as --custom-key and offers no stdin"
  say "form, so for the lifetime of that process the token is readable in /proc/*/cmdline"
  say "-- by any other process of your uid, and by root. That is the whole exposure: it is"
  say "not logged, not echoed, and not written to any file in this repository; the value"
  say "is stored in the project's Key Vault. Do not run this step on a shared machine."
  confirm "Continue with step 5 on this machine" \
    || die "stopped before creating the PAT connection." \
           "Re-run this step on a machine you trust:  ./scripts/setup.sh --only 5"
  say ""
  say "  [1] Mint one now with the Azure DevOps token lifecycle API (uses your az login)"
  say "  [2] Paste one you created in the Azure DevOps UI"
  local pat=''
  if [ "$(ask "Choose" "1")" = "2" ]; then
    pat="$(ask_secret "Paste the PAT (input hidden)")"
    [ -n "${pat}" ] || die "no token entered"
  else
    pat="$(mint_pat "${org}")"
  fi

  # The token is on this command line -- see the disclosure in the file header and the
  # prompt above. `azd ai connection create` has no stdin form for --custom-key, so this
  # is not fixable here; it is disclosed instead. The value lands in the project's Key
  # Vault, not in .azure/.
  azd ai connection create "${ADO_CONNECTION_NAME}" \
    --kind remote-tool \
    --target "https://dev.azure.com/${org}" \
    --auth-type custom-keys \
    --custom-key "pat=${pat}" \
    --force >/dev/null \
    || die "azd ai connection create ${ADO_CONNECTION_NAME} failed" \
           "You need a Foundry role that can write project connections."
  unset pat
  # Recorded only on the branch that actually created the connection, exactly like the
  # two AcrPull markers. A connection this script adopted -- `existing` was non-empty and
  # the answer above was "yes, replace the token" -- belongs to whoever made it, and may
  # be the one another deployment's agent reads its PAT from. teardown.sh deletes this
  # connection only when this marker says setup.sh created it; without the marker it
  # leaves it alone, because deleting it leaves a live agent failing every run with
  # ado_forbidden and nothing on screen explaining why.
  if [ -z "${existing}" ]; then
    env_set T2PR_SETUP_CREATED_ADO_CONNECTION "true"
  fi
  ok "connection ${ADO_CONNECTION_NAME} -> https://dev.azure.com/${org}"
  say "the token is stored in the project's Key Vault, not in .azure/<env>/.env"
}

# mint_pat <org> -> prints the token on stdout and nothing else.
# POST https://vssps.dev.azure.com/{org}/_apis/tokens/pats?api-version=7.1-preview.1
# authenticated with an Entra token for the Azure DevOps resource. Verified against a
# live organization: the resulting token's scope reads "vso.code_write vso.work".
mint_pat() {
  local org="$1" token valid_to name response pat
  token="$(az account get-access-token --resource "${ADO_RESOURCE_ID}" --query accessToken -o tsv 2>/dev/null || true)"
  [ -n "${token}" ] || die "could not get an Azure DevOps token for your az login" \
    "Your account must be a member of the '${org}' organization."

  name="$(ask "Token display name" "ticket-to-pr-agent")"
  local days; days="$(ask "Valid for how many days" "30")"
  valid_to="$(python3 -c '
import datetime, sys
print((datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))
' "${days}")"

  # vso.work is read-only on work items; vso.code_write covers cloning, pushing and pull
  # requests. allOrgs:false pins the token to this organization.
  local body_file; body_file="$(mktemp "${TMPDIR:-/tmp}/t2pr-pat.XXXXXX")"
  python3 -c '
import json, sys
print(json.dumps({"displayName": sys.argv[1], "scope": "vso.work vso.code_write",
                  "validTo": sys.argv[2], "allOrgs": False}))
' "${name}" "${valid_to}" > "${body_file}"

  # The access token goes in on stdin as a curl config file (-K -), never as -H on the
  # command line: argv is world-readable through /proc/*/cmdline for the life of the
  # process, and this is a bearer token for the Azure DevOps resource.
  response="$(printf 'header = "Authorization: Bearer %s"\n' "${token}" \
    | curl -K - -sS -X POST \
        -H "Content-Type: application/json" \
        --data "@${body_file}" \
        "https://vssps.dev.azure.com/${org}/_apis/tokens/pats?api-version=7.1-preview.1" || true)"
  rm -f "${body_file}"

  pat="$(printf '%s' "${response}" | jget patToken.token)"
  if [ -z "${pat}" ]; then
    local err; err="$(printf '%s' "${response}" | jget patTokenError)"
    die "the Azure DevOps token API did not return a token${err:+ (${err})}" \
      "Common causes: your account is not a member of '${org}'; the organization" \
      "has a policy that forbids PAT creation; or your tenant requires an interactive" \
      "reauthentication. Create the token in the Azure DevOps UI and re-run with [2]."
  fi

  # Not a secret -- teardown.sh uses it to revoke the token.
  local auth_id; auth_id="$(printf '%s' "${response}" | jget patToken.authorizationId)"
  [ -n "${auth_id}" ] && env_set T2PR_SETUP_PAT_AUTHORIZATION_ID "${auth_id}"
  env_set T2PR_SETUP_PAT_ORG "${org}"
  printf '%s' "${pat}"
}

# --------------------------------------------------------------------------------------
# Step 6 -- the ${{connections...}} indirection
# --------------------------------------------------------------------------------------
#
# This looks like a mistake and is not. azure.yaml cannot hold
#
#     T2PR_ADO_PAT: ${{connections.ado-secrets.credentials.pat}}
#
# directly: azd runs its own ${VAR} substitution over azure.yaml first and chokes on the
# doubled brace form with "unable to parse variable name". So azure.yaml holds
# ${T2PR_ADO_PAT}, and the azd environment variable T2PR_ADO_PAT holds the placeholder
# *text*. azd copies it through verbatim; the platform resolves it against the Foundry
# connection when the container starts. The azd environment never holds the token.
# shellcheck disable=SC2016  # the literal text is the point; it must not expand here.
PAT_PLACEHOLDER='${{connections.ado-secrets.credentials.pat}}'

step6_placeholder() {
  banner "6/9  The \${{connections...}} indirection"

  if [ "$(env_get T2PR_ADO_PAT)" = "${PAT_PLACEHOLDER}" ]; then
    skip "T2PR_ADO_PAT placeholder"
  else
    env_set T2PR_ADO_PAT "${PAT_PLACEHOLDER}"
    # Stamped only when the value actually changed. Step 7 compares this against the stamp
    # it records after a deploy: a placeholder newer than the last deploy means the
    # deployed agent version was baked without it and has to be replaced. Stamping on
    # every pass instead would make an ordinary idempotent re-run redeploy the agent,
    # which is minutes of rebuild for nothing.
    env_set T2PR_SETUP_PLACEHOLDER_SET_AT "$(date +%s)"
    ok "T2PR_ADO_PAT set to the connection placeholder (not to a token)"
  fi
  say "azure.yaml passes \${T2PR_ADO_PAT} to the container; azd substitutes this literal"
  say "text; the platform swaps it for the Key Vault value at container start."
}

# --------------------------------------------------------------------------------------
# Step 7 -- first deploy, then model access for the agent's instance identity
# --------------------------------------------------------------------------------------
#
# The agent's Entra identity does not exist until the first deploy creates an agent
# version, and azd assigns it NOTHING. Without a model role on the Foundry ACCOUNT the
# first real run dies with:
#   401 Principal does not have access to API/Operation
#
# VERIFIED MINIMUM: "Cognitive Services User" at account scope, on its own, is enough.
# Confirmed by removing every other role from a working deployment and running a full
# job -- fetch, clone, code, push, pull request -- which succeeded in 30 seconds.
#
# The alternates below are listed only as a fallback for a differently-configured tenant.
# If you see a 401 straight after granting, wait: role assignments take 30-150 seconds to
# take effect, and an impatient retry looks exactly like an insufficient role. That
# mistaken diagnosis is what put three redundant roles on this identity in the first place.
#
# Account scope, not project scope, because the Claude Code harness talks to the
# account-level /anthropic route. See docs/deploy.md.
AGENT_MODEL_ROLE="Cognitive Services User"
AGENT_MODEL_ROLE_ALTERNATES="Cognitive Services OpenAI User, Foundry User"

# The precondition, and the reason this step moved to seventh.
#
# A hosted agent's environment variables are baked into the agent VERSION at deploy time
# -- `azd ai agent show` returns them under definition.environment_variables, and nothing
# re-reads the azd environment afterwards. config.py refuses to start when T2PR_ADO_PAT,
# T2PR_HARNESS, T2PR_MODEL_DEPLOYMENT or T2PR_ALLOWED_ORGS is missing, so a version
# deployed without one of them never answers /readiness, never reaches `active`, and
# cannot be repaired except by deploying again.
#
# This runs on every entry to step 7, including `--only 7` and `--from 7`, because those
# are exactly the paths that skip the steps that set these.
DEPLOY_ENV_KEYS=(T2PR_HARNESS T2PR_MODEL_DEPLOYMENT T2PR_ALLOWED_ORGS T2PR_ADO_PAT)

require_deploy_preconditions() {
  local key value missing=()
  for key in "${DEPLOY_ENV_KEYS[@]}"; do
    value="$(env_get "${key}")"
    [ -n "${value}" ] || missing+=("${key}")
  done

  if [ "${#missing[@]}" -gt 0 ]; then
    die "cannot deploy: ${missing[*]} $([ "${#missing[@]}" -eq 1 ] && printf 'is' || printf 'are') not set in the azd environment" \
        "These are baked into the agent version at deploy time and the container refuses" \
        "to start without them, so the version would never pass its readiness probe and" \
        "no later step would repair it." \
        "" \
        "  T2PR_HARNESS, T2PR_MODEL_DEPLOYMENT, T2PR_ALLOWED_ORGS   step 2" \
        "  T2PR_ADO_PAT (the connection placeholder)                steps 5 and 6" \
        "" \
        "Run the steps that set them:" \
        "  ./scripts/setup.sh --from 2"
  fi

  # Set by step 3 when you declined to create the model deployment. Deploying anyway
  # produces an agent that 404s on every single run for the life of the version.
  if [ "$(env_get T2PR_SETUP_MODEL_DEPLOYMENT_MISSING)" = "true" ]; then
    die "cannot deploy: model deployment '$(env_get T2PR_MODEL_DEPLOYMENT)' does not exist" \
        "Step 3 recorded that you declined to create it. Every run of the deployed agent" \
        "would fail with a 404 from the model endpoint." \
        "Create it in ai.azure.com, then clear the marker:" \
        "  azd env set T2PR_SETUP_MODEL_DEPLOYMENT_MISSING false" \
        "or let step 3 create it:  ./scripts/setup.sh --only 3"
  fi

  [ -n "$(env_get FOUNDRY_PROJECT_ENDPOINT)" ] \
    || die "cannot deploy: FOUNDRY_PROJECT_ENDPOINT is not set -- run step 3 first" \
           "  ./scripts/setup.sh --from 3"
}

# agent_env_drift <agent-json> <KEY=value>... -- prints the names of the baked variables
# whose value in the DEPLOYED version differs from the one in the azd environment, one per
# line, and exits 0. Exits 9 when the version does not expose its environment at all, so
# the caller falls back to the timestamps rather than guessing.
#
# An absent key and an empty value are both drift, and deliberately so: a version deployed
# before step 5 and step 6 ran carries T2PR_ADO_PAT="" or no key at all, and that is
# exactly the version that has to be replaced.
agent_env_drift() {
  local json="$1"; shift
  printf '%s' "${json}" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(9)
baked = (doc.get("definition") or {}).get("environment_variables")
if not isinstance(baked, dict):
    sys.exit(9)
for pair in sys.argv[1:]:
    key, _, want = pair.partition("=")
    if (baked.get(key) or "") != want:
        print(key)
' "$@"
}

step7_deploy_identity() {
  banner "7/9  Deploy, then model access for the agent identity"

  require_deploy_preconditions

  local agent_json principal placeholder_at deployed_at redeploy=0 why=''
  agent_json="$(azd ai agent show --output json 2>/dev/null || true)"
  principal="$(printf '%s' "${agent_json}" | jget instance_identity.principal_id || true)"

  if [ -z "${principal}" ]; then
    redeploy=1; why="no agent version yet"
  else
    # What the DEPLOYED version was built with, not what the azd environment says now.
    # Environment variables are baked into the agent version and nothing re-reads them, so
    # this is the only authoritative answer to "does the running version match this
    # configuration". It catches the half-finished run P0-1 described -- a version built
    # before the PAT existed -- and also a plain edit to T2PR_ALLOWED_ORGS.
    local key expect=() drift='' drift_rc=0
    for key in "${DEPLOY_ENV_KEYS[@]}"; do expect+=("${key}=$(env_get "${key}")"); done
    drift="$(agent_env_drift "${agent_json}" "${expect[@]}")" || drift_rc=$?

    if [ "${drift_rc}" -eq 0 ] && [ -n "${drift}" ]; then
      redeploy=1
      why="the deployed version was built with a different $(printf '%s' "${drift}" | tr '\n' ' ')"
    else
      # Either everything matched, or this azd version does not report the deployed
      # environment (drift_rc 9). The timestamps are the fallback: step 6 stamps when it
      # writes the placeholder, step 7 stamps after a successful deploy.
      placeholder_at="$(env_get T2PR_SETUP_PLACEHOLDER_SET_AT)"
      deployed_at="$(env_get T2PR_SETUP_DEPLOYED_AT)"
      case "${placeholder_at}" in ''|*[!0-9]*) placeholder_at=0 ;; esac
      case "${deployed_at}"    in ''|*[!0-9]*) deployed_at=0 ;; esac
      if [ "${placeholder_at}" -gt "${deployed_at}" ]; then
        redeploy=1
        why="step 6 recorded the placeholder after the last deploy"
      fi
    fi
  fi

  if [ "${redeploy}" -eq 1 ]; then
    say "${why} -- running 'azd deploy' (5-15 minutes the first time)"
    azd deploy || die "azd deploy failed" \
      "If it says '[ImageError] Container registry authentication failed', step 4's" \
      "AcrPull assignments have not propagated yet -- wait a minute and re-run:" \
      "  ./scripts/setup.sh --from 7"
    env_set T2PR_SETUP_DEPLOYED_AT "$(date +%s)"
    agent_json="$(azd ai agent show --output json 2>/dev/null || true)"
    principal="$(printf '%s' "${agent_json}" | jget instance_identity.principal_id || true)"
  else
    skip "agent version deployed with the current configuration"
  fi

  [ -n "${principal}" ] || die "could not read the agent's instance identity" \
    "Run 'azd ai agent show --output json' and look for instance_identity.principal_id."
  ok "agent instance identity ${principal}"

  local endpoint account rg sub scope
  endpoint="$(env_get FOUNDRY_PROJECT_ENDPOINT)"
  account="$(printf '%s' "${endpoint}" | sed -E 's#^https://([^.]+)\..*#\1#')"
  rg="$(az cognitiveservices account list --query "[?name=='${account}'].resourceGroup | [0]" -o tsv)"
  sub="$(az account show --query id -o tsv)"
  scope="/subscriptions/${sub}/resourceGroups/${rg}/providers/Microsoft.CognitiveServices/accounts/${account}"

  role_assign "${principal}" ServicePrincipal "${AGENT_MODEL_ROLE}" "${scope}"
  env_set T2PR_SETUP_AGENT_PRINCIPAL_ID "${principal}"
  env_set T2PR_SETUP_FOUNDRY_ACCOUNT_SCOPE "${scope}"

  say ""
  say "If a run still fails with '401 Principal does not have access to API/Operation',"
  say "add one of: ${AGENT_MODEL_ROLE_ALTERNATES}"
  say "on the same scope, then re-run. If that fixes it, please open an issue saying"
  say "which one -- this script would rather grant exactly one role."
}

# --------------------------------------------------------------------------------------
# Step 8 -- the Entra SPA app registration for the Azure DevOps extension
# --------------------------------------------------------------------------------------
#
# Five things have to be true, and each missing one produces a different error:
#
#   * SPA redirect https://dev.azure.com/_public/_MsalPopup -- the Azure DevOps nested
#     app authentication broker's redirect. The page must not set redirectUri itself.
#   * No client secret. The .vsix is downloadable by anyone who can install it.
#   * signInAudience AzureADMultipleOrgs. Single-tenant fails under nested app auth with
#     AADSTS50194, because the Azure DevOps host frame brokers the token on /common and
#     ignores the child app's authority.
#   * The delegated user_impersonation permission on Azure Machine Learning Services.
#   * A service principal in this tenant AND an admin oauth2PermissionGrant. MSAL asks
#     for `.default`, and `.default` returns only permissions that are already consented
#     -- with no grant it returns nothing and the token has no usable scope.
step8_entra_app() {
  banner "8/9  Entra app registration for the Azure DevOps extension"

  local app_id object_id tenant adopted=0
  tenant="$(az account show --query tenantId -o tsv)"
  app_id="$(env_get T2PR_ENTRA_CLIENT_ID)"
  if [ -n "${app_id}" ]; then
    # Recorded by a previous run of this step (or set by hand by an administrator who
    # created the app for you). Either way this script did not create it *now*, so the
    # T2PR_SETUP_CREATED_ENTRA_APP marker is left exactly as it already stands.
    skip "app registration from T2PR_ENTRA_CLIENT_ID (${app_id})"
  else
    # Display name is NOT unique in a tenant, so a match here is a candidate, never an
    # identification. Adopting one silently used to be possible, and the PATCH below is
    # not read-only: it rewrites signInAudience to multi-tenant, replaces the SPA redirect
    # URIs and blanks the web and publicClient ones. Doing that to somebody else's app
    # registration because it happens to share a name is not a recoverable mistake.
    app_id="$(az ad app list --display-name "${ENTRA_APP_DISPLAY_NAME}" --query '[0].appId' -o tsv 2>/dev/null || true)"
    if [ -n "${app_id}" ] && [ "${app_id}" != "None" ]; then
      adopted=1
    else
      app_id="$(az ad app create --display-name "${ENTRA_APP_DISPLAY_NAME}" \
                  --sign-in-audience AzureADMultipleOrgs --query appId -o tsv 2>/dev/null || true)"
      [ -n "${app_id}" ] || die "could not create the '${ENTRA_APP_DISPLAY_NAME}' app registration" \
        "Your tenant may restrict app registration to administrators. Ask one to create" \
        "a multi-tenant SPA app with redirect ${NAA_REDIRECT_URI} and pass its client id" \
        "back with: azd env set T2PR_ENTRA_CLIENT_ID <appId>"
      # The one branch that may set this. teardown.sh deletes the app only when it is
      # true, so recording it anywhere else would arm a delete of somebody else's app.
      env_set T2PR_SETUP_CREATED_ENTRA_APP "true"
      ok "created app registration ${app_id}"
    fi
  fi

  if [ "${adopted}" -eq 1 ]; then
    local app_display app_created
    app_display="$(az ad app show --id "${app_id}" --query displayName -o tsv 2>/dev/null || true)"
    app_created="$(az ad app show --id "${app_id}" --query createdDateTime -o tsv 2>/dev/null || true)"
    warn "an app registration named '${ENTRA_APP_DISPLAY_NAME}' already exists in this tenant,"
    warn "and setup.sh did not create it. Display names are not unique."
    say "  appId       ${app_id}"
    say "  displayName ${app_display}"
    say "  created     ${app_created}"
    say ""
    say "Adopting it REWRITES it: signInAudience -> AzureADMultipleOrgs, spa.redirectUris"
    say "-> [${NAA_REDIRECT_URI}], web and publicClient redirect URIs -> empty, plus a"
    say "tenant-wide (AllPrincipals) consent grant for user_impersonation."
    say "Check the appId above in Entra ID -> App registrations before answering."
    confirm "Reconfigure this existing app registration" \
      || die "not adopting ${app_id}." \
             "Create a separate app and record it, then re-run step 8:" \
             "  azd env set T2PR_ENTRA_CLIENT_ID <appId>"
    ok "adopting existing app registration ${app_id} (not created here; teardown will not delete it)"
  fi

  object_id="$(az ad app show --id "${app_id}" --query id -o tsv)"

  # One idempotent PATCH: audience, SPA redirect, and the API permission. `az ad app
  # update` has no --spa-redirect-uris, hence Graph directly.
  local patch_file; patch_file="$(mktemp "${TMPDIR:-/tmp}/t2pr-app.XXXXXX")"
  python3 -c '
import json, sys
print(json.dumps({
    "signInAudience": "AzureADMultipleOrgs",
    "spa": {"redirectUris": [sys.argv[1]]},
    "web": {"redirectUris": []},
    "publicClient": {"redirectUris": []},
    "requiredResourceAccess": [{
        "resourceAppId": sys.argv[2],
        "resourceAccess": [{"id": sys.argv[3], "type": "Scope"}],
    }],
}))
' "${NAA_REDIRECT_URI}" "${AML_APP_ID}" "${AML_USER_IMPERSONATION_SCOPE_ID}" > "${patch_file}"
  az rest --method patch \
    --url "https://graph.microsoft.com/v1.0/applications/${object_id}" \
    --headers "Content-Type=application/json" \
    --body "@${patch_file}" >/dev/null \
    || { rm -f "${patch_file}"; die "could not configure the app registration" \
         "You need Application Administrator, or ownership of ${app_id}."; }
  rm -f "${patch_file}"
  ok "SPA redirect ${NAA_REDIRECT_URI}, audience AzureADMultipleOrgs, user_impersonation"

  # There must be no secret. A published .vsix cannot hold one.
  local secret_count
  secret_count="$(az ad app show --id "${app_id}" --query 'length(passwordCredentials)' -o tsv 2>/dev/null || echo 0)"
  if [ "${secret_count:-0}" != "0" ]; then
    warn "this app has ${secret_count} client secret(s). It must have none -- the .vsix is"
    warn "downloadable by anyone who can install it. Delete them in the portal."
  else
    ok "no client secret"
  fi

  # The service principal. Without it there is nothing to consent to and nothing for
  # `.default` to read.
  local sp_object_id
  sp_object_id="$(az ad sp show --id "${app_id}" --query id -o tsv 2>/dev/null || true)"
  if [ -z "${sp_object_id}" ]; then
    az ad sp create --id "${app_id}" >/dev/null \
      || die "could not create the service principal for ${app_id}" \
             "Ask an administrator to run: az ad sp create --id ${app_id}"
    sp_object_id="$(az ad sp show --id "${app_id}" --query id -o tsv)"
    ok "created service principal ${sp_object_id}"
  else
    skip "service principal ${sp_object_id}"
  fi

  # The resource side needs a service principal in this tenant too.
  local resource_sp_id
  resource_sp_id="$(az ad sp show --id "${AML_APP_ID}" --query id -o tsv 2>/dev/null || true)"
  if [ -z "${resource_sp_id}" ]; then
    az ad sp create --id "${AML_APP_ID}" >/dev/null \
      || die "could not create the service principal for Azure Machine Learning Services" \
             "Ask an administrator to run: az ad sp create --id ${AML_APP_ID}"
    resource_sp_id="$(az ad sp show --id "${AML_APP_ID}" --query id -o tsv)"
  fi

  # Tenant-wide consent. `.default` returns only already-consented permissions, so
  # without this the token comes back with no usable scope and the agent returns 401.
  local grant_id
  grant_id="$(az rest --method get \
      --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '${sp_object_id}'" \
      -o json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    for g in json.load(sys.stdin).get("value", []):
        if g.get("resourceId") == sys.argv[1] and "user_impersonation" in (g.get("scope") or ""):
            print(g["id"]); break
except Exception:
    pass
' "${resource_sp_id}" || true)"

  if [ -n "${grant_id}" ]; then
    skip "admin consent grant ${grant_id}"
  else
    local grant_file; grant_file="$(mktemp "${TMPDIR:-/tmp}/t2pr-grant.XXXXXX")"
    python3 -c '
import json, sys
print(json.dumps({"clientId": sys.argv[1], "consentType": "AllPrincipals",
                  "resourceId": sys.argv[2], "scope": "user_impersonation"}))
' "${sp_object_id}" "${resource_sp_id}" > "${grant_file}"
    az rest --method post \
      --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" \
      --headers "Content-Type=application/json" \
      --body "@${grant_file}" >/dev/null \
      || { rm -f "${grant_file}"; die "could not grant admin consent for user_impersonation" \
           "You need Privileged Role Administrator or Cloud Application Administrator." \
           "An administrator can do it in the portal: Entra ID -> App registrations ->" \
           "${ENTRA_APP_DISPLAY_NAME} -> API permissions -> Grant admin consent."; }
    rm -f "${grant_file}"
    ok "admin consent granted for user_impersonation (AllPrincipals)"
  fi

  # These two key names are load-bearing: extension/scripts/configure.mjs resolves
  # T2PR_ENTRA_CLIENT_ID and T2PR_ENTRA_TENANT_ID from the azd environment, so a later
  # `npm run configure` in extension/ needs no arguments beyond the publisher id.
  env_set T2PR_ENTRA_CLIENT_ID "${app_id}"
  env_set T2PR_ENTRA_TENANT_ID "${tenant}"
}

# --------------------------------------------------------------------------------------
# Step 9 -- extension config, packaging, summary
# --------------------------------------------------------------------------------------
#
# The path to the .vsix build_extension_vsix produced, or empty. A global because the
# function prints progress as it goes and so cannot hand the path back on stdout.
EXT_VSIX_PATH=''

# build_extension_vsix <publisher> <repo-url> -- 0 when a .vsix exists on disk afterwards.
#
# What this actually does, stated plainly, because an earlier version of this comment
# claimed it "reaches nothing outside this repository" and that was false:
#
#   - `npm ci` DOES reach the network. It downloads 318 packages from registry.npmjs.org,
#     every one pinned by package-lock.json to a version and an integrity hash.
#   - It runs as the deployer's uid, in a run where they have just minted a live Azure
#     DevOps PAT and where ~/.azure holds refresh tokens for an identity that can register
#     subscription providers and mint organization PATs. So `--ignore-scripts` is not
#     optional here: without it, any of those 318 packages' preinstall/install/postinstall
#     scripts executes third-party code with that reach. Only esbuild declares an install
#     script, and its platform binary arrives as an optionalDependency the lockfile already
#     pins, so the build does not need it -- `npm ci --ignore-scripts` then typecheck,
#     build and tfx all succeed from a clean tree.
#   - configure.mjs and the build itself really do stay local: configure.mjs reads the azd
#     environment steps 7 and 8 wrote, the build bundles TypeScript, and tfx zips a
#     directory. Those need no credential this script does not already hold.
#
# On that basis it is run here rather than printed.
#
# UPLOADING is the line this stops at, and that is deliberate. Publishing to the
# Marketplace needs a Marketplace personal access token -- a second credential, unrelated
# to the Azure DevOps PAT in step 5 -- and this script takes exactly one secret. Step 9
# prints the upload, share and install steps instead.
#
# Nothing here is ever fatal. By the time step 9 runs the agent is deployed and answering;
# a host without Node, a publisher id the deployer has not decided on yet, or a tfx that
# dislikes the manifest are all reasons to build the package elsewhere, not reasons to
# fail a deployment that succeeded.
build_extension_vsix() {
  local publisher="$1" repo_url="$2" ext="${REPO_ROOT}/extension" log id version vsix
  EXT_VSIX_PATH=''

  # configure.mjs refuses to write a manifest that still carries a REPLACE-WITH-
  # placeholder, so both answers are required, not just the publisher. Blank is a
  # legitimate answer to either -- "I will decide later" -- not an error.
  if [ -z "${publisher}" ] || [ -z "${repo_url}" ]; then
    say "Skipping the .vsix build: no $( [ -z "${publisher}" ] && printf 'publisher id' || printf 'repository URL' ) yet."
    return 1
  fi
  if ! have node || ! have npm; then
    say "Skipping the .vsix build: no node/npm on this machine."
    return 1
  fi

  log="$(mktemp "${TMPDIR:-/tmp}/t2pr-ext-build.XXXXXX")" || return 1

  # `npm ci` deletes node_modules and reinstalls from the lockfile every time it runs.
  # npm writes node_modules/.package-lock.json as it installs, so a package-lock.json
  # newer than that marker is the one honest signal that the tree is stale. Anything
  # else -- a missing marker, a missing node_modules -- reinstalls.
  if [ -f "${ext}/node_modules/.package-lock.json" ] \
     && [ ! "${ext}/package-lock.json" -nt "${ext}/node_modules/.package-lock.json" ]; then
    skip "extension dependencies (npm ci)"
  else
    say "extension: npm ci --ignore-scripts..."
    if ! ( cd "${ext}" && npm ci --ignore-scripts ) >"${log}" 2>&1; then
      warn "npm ci failed in extension/"
      sed -e 's/^/      /' "${log}" | tail -20 >&2
      rm -f "${log}"
      return 1
    fi
    ok "extension dependencies installed"
  fi

  # Both values are passed explicitly even though configure.mjs would find them in the
  # azd environment: this step wrote them a moment ago, and an argument cannot be read
  # back wrong.
  say "extension: npm run configure..."
  if ! ( cd "${ext}" && npm run configure -- --publisher "${publisher}" --repo-url "${repo_url}" ) \
       >"${log}" 2>&1; then
    warn "npm run configure failed in extension/"
    sed -e 's/^/      /' "${log}" | tail -20 >&2
    rm -f "${log}"
    return 1
  fi
  ok "extension configured from this azd environment"

  say "extension: npm run package..."
  if ! ( cd "${ext}" && npm run package ) >"${log}" 2>&1; then
    warn "npm run package failed in extension/"
    sed -e 's/^/      /' "${log}" | tail -20 >&2
    rm -f "${log}"
    return 1
  fi
  rm -f "${log}"

  # Verify the artifact rather than trust the exit status. tfx names it
  # <publisher>.<id>-<version>.vsix from the manifest configure.mjs just generated, so
  # the name is derived, not guessed.
  id="$(jget id <"${ext}/vss-extension.local.json")"
  version="$(jget version <"${ext}/vss-extension.local.json")"
  vsix="${ext}/dist-vsix/${publisher}.${id}-${version}.vsix"
  if [ ! -f "${vsix}" ]; then
    warn "npm run package succeeded but ${vsix#"${REPO_ROOT}/"} is not there"
    return 1
  fi
  EXT_VSIX_PATH="${vsix}"
  ok "built ${vsix#"${REPO_ROOT}/"}"
  return 0
}

step9_finish() {
  banner "9/9  Extension configuration and summary"

  local invocations client_id tenant publisher repo_url
  invocations="$(env_get AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT)"
  [ -n "${invocations}" ] || die "AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT is not set" \
    "It is written by a successful deploy. Run: ./scripts/setup.sh --from 7"
  client_id="$(env_get T2PR_ENTRA_CLIENT_ID)"
  tenant="$(env_get T2PR_ENTRA_TENANT_ID)"
  [ -n "${client_id}" ] || warn "no T2PR_ENTRA_CLIENT_ID -- run step 8 before packaging"

  # The extension configures ITSELF from this azd environment. extension/scripts/
  # configure.mjs reads AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT, T2PR_ENTRA_CLIENT_ID,
  # T2PR_ENTRA_TENANT_ID, T2PR_EXT_PUBLISHER and T2PR_EXT_REPO_URL, and writes the
  # tenant-specific values into two gitignored files -- extension/deploy.local.json and
  # extension/vss-extension.local.json. So this step asks for the two answers the azd
  # environment cannot supply, records them, and then runs the build; it never writes
  # agent-config.json or the manifest itself.
  #
  # `azure.yaml` is the one exception to that pattern, and the warning below is not
  # decoration: on the connect path step 3 wrote the Foundry endpoint into it, and it is
  # tracked. See the note on yaml_set_endpoint for why it cannot be a variable.
  say "The extension reads its settings from this azd environment at package time."
  say "Two values are still missing from it, and only you know them."
  say ""
  say "Marketplace publisher id: the short immutable id from"
  say "https://marketplace.visualstudio.com/manage -- e.g. contoso-devtools, not the"
  say "display name. tfx names the .vsix after it and refuses one you do not own."
  publisher="$(ask "T2PR_EXT_PUBLISHER (blank to set it later)" "$(env_get T2PR_EXT_PUBLISHER || true)")"
  [ -z "${publisher}" ] || env_set T2PR_EXT_PUBLISHER "${publisher}"

  say ""
  say "Repository URL for the extension's Marketplace Repository and Support links."
  repo_url="$(ask "T2PR_EXT_REPO_URL (blank to set it later)" "$(env_get T2PR_EXT_REPO_URL || true)")"
  [ -z "${repo_url}" ] || env_set T2PR_EXT_REPO_URL "${repo_url}"

  # Build it. Everything up to the upload is deterministic and needs nothing this script
  # does not already have, so printing it would just be homework. See build_extension_vsix
  # for why the upload is where this stops.
  printf '\n  %sThe Azure DevOps extension%s\n' "${C_BOLD}" "${C_OFF}"
  if build_extension_vsix "${publisher}" "${repo_url}"; then
    local vsix_rel="${EXT_VSIX_PATH#"${REPO_ROOT}/"}"
    cat <<EOF

    ${vsix_rel}

EOF
  else
    local pub_show="${publisher:-<your-publisher-id>}"
    local repo_show="${repo_url:-https://github.com/<your-org>/ticket-to-pr-agent}"
    cat <<EOF

  Build it on any machine with Node LTS that can read this azd environment:

    cd extension
    npm ci --ignore-scripts    # the build needs no package's install script; see step 9
    npm run configure -- --publisher ${pub_show} --repo-url ${repo_show}
    npm run package     # -> dist-vsix/${pub_show}.ticket-to-pr-*.vsix

  \`npm run configure\` picks the agent URL, Entra client id and tenant id out of the azd
  environment this script just wrote, so those three need no arguments.

EOF
  fi

  cat <<EOF
  ${C_BOLD}Upload, share and install -- this part stays manual${C_OFF}

  Publishing needs a Marketplace personal access token. That is a second credential,
  unrelated to the Azure DevOps PAT in step 5, and this script takes exactly one secret.

    1. https://marketplace.visualstudio.com/manage -> your publisher ->
       New extension -> Azure DevOps, and upload the .vsix.
    2. Wait about 100 seconds for Marketplace verification. Until it clears, the
       extension's page returns 404 and the management API answers
       ExtensionDoesNotExistException -- which reads exactly like a failed upload.
       It is not. Wait.
    3. On the extension, ... -> Share/Unshare, and add your Azure DevOps organization.
    4. Install it into that organization -- needs the Manage extensions permission.

  Full walkthrough: docs/extension.md.

  ${C_BOLD}Smoke-test the agent${C_OFF}

    BASE=\$(azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT)
    printf 'header = "Authorization: Bearer %s"\\n' \\
      "\$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)" \\
      | curl -K - -si -X POST "\$BASE" \\
          -H 'Content-Type: application/json' -d @request.json

  The token goes in on stdin as a curl config file rather than as -H on the command
  line, for the same reason step 5's token call does: argv is readable through
  /proc/*/cmdline by every process of your uid for the life of the request, and that is
  a bearer token for your own identity.

  \$BASE already carries ?api-version=v1 -- every gateway call needs it. Poll with the
  x-agent-invocation-id response header, not the runId in the body; the gateway rejects
  anything else with "400 Invalid invocation ID format".

EOF

  # Against HEAD, not against the index. `git diff -- <path>` compares the working tree
  # with the *index*, so a `git add -A` -- the last thing anybody does before committing,
  # and the exact moment this warning matters most -- makes it fall silent.
  if ! git -C "${REPO_ROOT}" diff --quiet HEAD -- "${AZURE_YAML}" 2>/dev/null; then
    warn "${AZURE_YAML} is a TRACKED file and setup.sh modified it."
    say "It now carries your Foundry account and project names. azd will not read that"
    say "endpoint from a variable, so this could not be written anywhere untracked."
    say "Check it before you commit anything:"
    say "  git diff -- ${AZURE_YAML}"
    say "  git checkout -- ${AZURE_YAML}     # revert (breaks 'azd provision' on the"
    say "                                    # connect path until step 3 runs again)"
    say ""
  fi

  banner "What this created"
  # "(created here)" is what teardown.sh keys off: it deletes only what setup.sh made.
  local made_project made_model made_acr pat_org
  made_project="$( [ "$(env_get T2PR_SETUP_CREATED_PROJECT)" = "true" ] && printf 'created here' || printf 'pre-existing, left alone on teardown' )"
  made_model="$( [ "$(env_get T2PR_SETUP_CREATED_MODEL_DEPLOYMENT)" = "true" ] && printf '  (created here)' || true )"
  made_acr="$( [ "$(env_get T2PR_SETUP_CREATED_ACR)" = "true" ] && printf '  (created here)' || true )"
  local made_app
  made_app="$( [ "$(env_get T2PR_SETUP_CREATED_ENTRA_APP)" = "true" ] \
                 && printf '  (created here)' \
                 || printf '  (pre-existing, left alone on teardown)' )"
  local made_conn
  made_conn="$( [ "$(env_get T2PR_SETUP_CREATED_ADO_CONNECTION)" = "true" ] \
                 && printf 'created here' \
                 || printf 'pre-existing, left alone on teardown' )"
  pat_org="$(env_get T2PR_SETUP_PAT_ORG)"
  [ -n "${pat_org}" ] || pat_org="$(env_get T2PR_ALLOWED_ORGS | cut -d, -f1)"
  cat <<EOF
  azd environment          $(env_get AZURE_ENV_NAME)
  Foundry project          $(env_get FOUNDRY_PROJECT_ENDPOINT)
  Foundry project (ARM)    $(env_get AZURE_AI_PROJECT_ID)
                           ${made_project}
  model deployment         $(env_get T2PR_MODEL_DEPLOYMENT)${made_model}
  container registry       $(env_get AZURE_CONTAINER_REGISTRY_ENDPOINT)${made_acr}
  agent identity           $(env_get T2PR_SETUP_AGENT_PRINCIPAL_ID)
  agent model role         ${AGENT_MODEL_ROLE} on $(env_get T2PR_SETUP_FOUNDRY_ACCOUNT_SCOPE)
  ADO PAT connection       ${ADO_CONNECTION_NAME} -> https://dev.azure.com/${pat_org}
                           ${made_conn}
  Entra app (extension)    ${client_id} in tenant ${tenant}${made_app}

  ./scripts/teardown.sh removes all of it, in reverse, asking first.
EOF
}

# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------
STEPS=(step1_preflight step2_environment step3_project step4_registry step5_pat
       step6_placeholder step7_deploy_identity step8_entra_app step9_finish)

usage() { sed -n '2,51p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# step_number <flag> <argument> -- validates and echoes it, or dies.
# `--from` with no argument used to leave `shift 2` shifting past the end, which under
# `set -e` exits 1 silently; `--from x` reached `[ x -ge 1 ]`, which throws. Both looked
# like the script deciding on its own to do nothing.
step_number() {
  local flag="$1" arg="${2-}"
  case "${arg}" in
    '' ) die "${flag} needs a step number (1..${#STEPS[@]})" "Try --list." ;;
    *[!0-9]*) die "${flag} takes a step number, not '${arg}'" "Try --list." ;;
  esac
  [ "${arg}" -ge 1 ] && [ "${arg}" -le "${#STEPS[@]}" ] \
    || die "${flag} must be 1..${#STEPS[@]}, not ${arg}" "Try --list."
  printf '%s' "${arg}"
}

list_steps() {
  local i=1 s
  for s in "${STEPS[@]}"; do printf '  %d  %s\n' "${i}" "${s#step?_}"; i=$((i+1)); done
  printf '\n  Step N assumes steps 1..N-1 have already run against this azd environment.\n'
  printf '  --from N and --only N do not run them for you: they check what they need and\n'
  printf '  say which earlier step sets it. Step 7 (deploy) bakes T2PR_HARNESS,\n'
  printf '  T2PR_MODEL_DEPLOYMENT, T2PR_ALLOWED_ORGS and T2PR_ADO_PAT into the agent\n'
  printf '  version, so it refuses to run until steps 2, 5 and 6 have set all four.\n'
}

main() {
  local from=1 only=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) from="$(step_number --from "${2-}")" || exit 1; shift 2 ;;
      --only) only="$(step_number --only "${2-}")" || exit 1; shift 2 ;;
      --list) list_steps; exit 0 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option '$1'" "Try --help." ;;
    esac
  done

  printf '%sticket-to-pr-agent setup%s  --  repo %s\n' "${C_BOLD}" "${C_OFF}" "${REPO_ROOT}"
  [ "${PROMPT_SRC}" = "/dev/tty" ] || warn "no terminal -- prompts will read from stdin"

  if [ "${only}" -ne 0 ]; then
    # Steps 2..9 all read the azd environment, so make sure one is selected first, and
    # say so rather than letting every env_get come back empty.
    if [ "${only}" -ne 1 ]; then
      azd_values_refresh
      [ -n "$(env_get AZURE_ENV_NAME)" ] || die "no azd environment is selected" \
        "Step ${only} reads the environment steps 1 and 2 create." \
        "  ./scripts/setup.sh --from 1"
    fi
    "${STEPS[$((only-1))]}"
  else
    if [ "${from}" -ne 1 ]; then
      azd_values_refresh
      [ -n "$(env_get AZURE_ENV_NAME)" ] || die "no azd environment is selected" \
        "Step ${from} reads the environment steps 1 and 2 create." \
        "  ./scripts/setup.sh --from 1"
    fi
    local i="${from}"
    while [ "${i}" -le "${#STEPS[@]}" ]; do
      "${STEPS[$((i-1))]}"
      i=$((i+1))
    done
  fi

  printf '\n%sDone.%s\n' "${C_GREEN}${C_BOLD}" "${C_OFF}"
}

main "$@"
