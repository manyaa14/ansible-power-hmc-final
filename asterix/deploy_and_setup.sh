#!/bin/bash

# Asterix Deployment Automation Script
# This script deploys a vHMC, sets up Asterix emulator, and runs integration tests.
# If you want to reuse an existing HMC, comment out the deploy_vm playbook command below
# and make sure asterix/inventory plus asterix/host_vars/<hmc_ip>.yml are populated.

set -euo pipefail

export ANSIBLE_HOST_KEY_CHECKING=False
# Unset the SSH agent socket so the SSH client cannot offer agent keys before
# password auth. Without this, sshpass-based connections (both in shell tasks
# and inside Ansible modules like hmc_update_upgrade) exhaust the vHMC's
# MaxAuthTries limit and get disconnected before the password is ever attempted.
unset SSH_AUTH_SOCK

# Cross-platform sed -i: GNU sed (Linux/Jenkins) uses -i, BSD sed (macOS) needs -i ''
if sed --version 2>/dev/null | grep -q 'GNU'; then
    SED_I() { sed -i "$@"; }
else
    SED_I() { sed -i '' "$@"; }
fi

HSCROOT_PASSWORD=""
# Read DEPLOY_VM_ENABLED from environment so Jenkins can pass it in via withEnv.
# Defaults to false if not set (local runs / reuse existing HMC).
DEPLOY_VM_ENABLED="${DEPLOY_VM_ENABLED:-false}"
VHMC_INIT_WAIT_SECONDS=300

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INTEGRATION_DIR="${COLLECTION_ROOT}/tests/integration"
INTEGRATION_CONFIG_FILE="${INTEGRATION_DIR}/integration_config.yml"
ASTERIX_INVENTORY_FILE="${SCRIPT_DIR}/inventory"
ASTERIX_HOST_VARS_DIR="${SCRIPT_DIR}/host_vars"
REPORT_GENERATOR="${INTEGRATION_DIR}/generate_html_table_report_sequential.py"

MODULE_ORDER=(
    "hmc_command"
    "hmc_pwdpolicy"
    "hmc_user"
    "powervm_lpar_instance"
    "powervm_partition_profile"
    "powervm_virtual_switches"
    "vios"
    "powervm_dlpar"
    "vios_alt_root_vg"
    "vios_mapping_facts"
    "vios_secure"
    "vios_update_upgrade"
    "platform_update"
    "create_service_event"
    "powervm_lpar_migration"
    "powervm_virtual_network"
    "powervm_client_network_adapter"
    "hmc_update_upgrade"
    "power_system"
)

echo "=========================================="
echo "Asterix Deployment Automation"
echo "=========================================="

cd "${SCRIPT_DIR}"

# Patch main.yml with os_release and vm_name from Jenkins parameters (or local env vars).
# If Jenkins Active Choices returns "N/A ..." when DEPLOY_VM_ENABLED=false, skip the patch
# so main.yml keeps its default values — deploy_vm.yml won't run anyway in that case.
OS_RELEASE="${OS_RELEASE:-}"
VM_NAME="${VM_NAME:-}"
OPENCERT_PATH="${OPENCERT_PATH:-}"
if [ -n "${OS_RELEASE}" ] && [[ "${OS_RELEASE}" != N/A* ]]; then
    SED_I "s/^os_release:.*/os_release: \"${OS_RELEASE}\"/" "${SCRIPT_DIR}/vars/main.yml"
    echo "main.yml patched: os_release=${OS_RELEASE}"
fi
if [ -n "${VM_NAME}" ] && [[ "${VM_NAME}" != N/A* ]]; then
    SED_I "s/^vm_name:.*/vm_name: \"${VM_NAME}\"/" "${SCRIPT_DIR}/vars/main.yml"
    echo "main.yml patched: vm_name=${VM_NAME}"
fi
if [ -n "${OPENCERT_PATH}" ] && [[ "${OPENCERT_PATH}" != N/A* ]]; then
    SED_I "s|^opencert_path:.*|opencert_path: \"${OPENCERT_PATH}\"|" "${SCRIPT_DIR}/vars/main.yml"
    echo "main.yml patched: opencert_path=${OPENCERT_PATH}"
fi
echo ""

#Step 1: Optional vHMC deployment
echo "Step 1: Running deploy_vm.yml playbook to deploy vhmc if enabled..."
# Set DEPLOY_VM_ENABLED="true" when you want this script to deploy a fresh vHMC.
# If you want to reuse an existing HMC, keep DEPLOY_VM_ENABLED="false"
# and make sure ${ASTERIX_INVENTORY_FILE} plus ${ASTERIX_HOST_VARS_DIR}/<hmc_ip>.yml are populated.

if [ "${DEPLOY_VM_ENABLED}" = "true" ]; then
    ansible-playbook deploy_vm.yml

    echo ""
    echo "Waiting ${VHMC_INIT_WAIT_SECONDS} seconds for vHMC system services to stabilize..."
    sleep "${VHMC_INIT_WAIT_SECONDS}"
    echo "vHMC deployment step completed"
else
    echo "deploy_vm.yml execution skipped by script configuration"
fi

echo ""

# Step 2: Setup Asterix
echo "Step 2: Running asterix_full.yml playbook to deploy asterix on our vhmc..."
ansible-playbook -i inventory asterix_full.yml

echo ""
echo "Asterix setup completed"
echo ""

echo "Step 3: Collecting HMC IP and managed system name..."

if [ ! -f "${ASTERIX_INVENTORY_FILE}" ]; then
    echo "ERROR: Inventory file not found: ${ASTERIX_INVENTORY_FILE}"
    echo "Populate it manually or enable deploy_vm.yml execution."
    exit 1
fi

HMC_IP="$(awk '/^\[hmcs\]/{getline; print $1; exit}' "${ASTERIX_INVENTORY_FILE}")"

if [ -z "${HMC_IP}" ]; then
    echo "ERROR: Unable to determine HMC IP from ${ASTERIX_INVENTORY_FILE}"
    echo "Ensure the [hmcs] group contains exactly one HMC entry."
    exit 1
fi

HOST_VARS_FILE="${ASTERIX_HOST_VARS_DIR}/${HMC_IP}.yml"

if [ ! -f "${HOST_VARS_FILE}" ]; then
    echo "ERROR: Host vars file not found: ${HOST_VARS_FILE}"
    echo "Populate it manually with hscroot_password or enable deploy_vm.yml execution."
    exit 1
fi

HSCROOT_PASSWORD="$(awk -F': ' '/hscroot_password/{gsub(/"/, "", $2); print $2; exit}' "${HOST_VARS_FILE}")"

if [ -z "${HSCROOT_PASSWORD}" ]; then
    echo "ERROR: Unable to determine hscroot password from ${HOST_VARS_FILE}"
    exit 1
fi

ASTERIX_NAME="HV4"

echo "Detected HMC IP: ${HMC_IP}"
echo "Detected Asterix managed system: ${ASTERIX_NAME}"
echo ""

echo "Step 4: Updating integration configuration..."
cat > "${INTEGRATION_CONFIG_FILE}" <<EOF
---
inventory_hostname: ${HMC_IP}
curr_hmc_auth:
   username: hscroot
   password: ${HSCROOT_PASSWORD}
EOF

echo "Using HMC IP from ${ASTERIX_INVENTORY_FILE}"
echo "Using hscroot password from ${HOST_VARS_FILE}"

POWER_SYSTEM_VARS_FILE="${INTEGRATION_DIR}/targets/power_system/tasks/vars.yaml"
POWERVM_LPAR_INSTANCE_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_lpar_instance/tasks/vars.yaml"
POWERVM_PARTITION_PROFILE_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_partition_profile/tasks/vars.yaml"
POWERVM_VIRTUAL_SWITCHES_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_virtual_switches/tasks/vars.yaml"
VIOS_VARS_FILE="${INTEGRATION_DIR}/targets/vios/tasks/vars.yaml"
POWERVM_DLPAR_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_dlpar/tasks/vars.yaml"
VIOS_ALT_ROOT_VG_VARS_FILE="${INTEGRATION_DIR}/targets/vios_alt_root_vg/tasks/vars.yaml"
VIOS_MAPPING_FACTS_VARS_FILE="${INTEGRATION_DIR}/targets/vios_mapping_facts/tasks/vars.yaml"
VIOS_SECURE_VARS_FILE="${INTEGRATION_DIR}/targets/vios_secure/tasks/vars.yaml"
VIOS_UPDATE_UPGRADE_VARS_FILE="${INTEGRATION_DIR}/targets/vios_update_upgrade/tasks/vars.yaml"
PLATFORM_UPDATE_VARS_FILE="${INTEGRATION_DIR}/targets/platform_update/tasks/vars.yaml"
CREATE_SERVICE_EVENT_VARS_FILE="${INTEGRATION_DIR}/targets/create_service_event/tasks/vars.yaml"
POWERVM_LPAR_MIGRATION_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_lpar_migration/tasks/vars.yaml"
POWERVM_VIRTUAL_NETWORK_VARS_FILE="${INTEGRATION_DIR}/targets/powervm_virtual_network/tasks/vars.yaml"

SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${POWER_SYSTEM_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: \"${ASTERIX_NAME}\"/" "${POWERVM_LPAR_INSTANCE_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: \"${ASTERIX_NAME}\"/" "${POWERVM_PARTITION_PROFILE_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${POWERVM_VIRTUAL_SWITCHES_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${VIOS_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${POWERVM_DLPAR_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${VIOS_ALT_ROOT_VG_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${VIOS_MAPPING_FACTS_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${VIOS_SECURE_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${VIOS_UPDATE_UPGRADE_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${PLATFORM_UPDATE_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${CREATE_SERVICE_EVENT_VARS_FILE}"
SED_I "s/^src_system:.*/src_system: ${ASTERIX_NAME}/" "${POWERVM_LPAR_MIGRATION_VARS_FILE}"
SED_I "s/^system_name:.*/system_name: ${ASTERIX_NAME}/" "${POWERVM_VIRTUAL_NETWORK_VARS_FILE}"

echo "Integration configuration updated"
echo ""

echo "Step 5: Running integration tests sequentially..."
cd "${INTEGRATION_DIR}"

ALL_LOG_FILES=()

for module_name in "${MODULE_ORDER[@]}"; do
    log_file="./test_${module_name}-FVTR.log"
    echo "------------------------------------------"
    echo "Running integration tests for ${module_name}"
    echo "Log file: ${log_file}"
    echo "------------------------------------------"
    ANSIBLE_TEST_PYTHON_INTERPRETER="$(command -v python3)" \
        ansible-test integration "${module_name}" 2>&1 | tee "${log_file}"

    ALL_LOG_FILES+=("${log_file}")

    echo "Generating HTML report for ${module_name} from ${log_file}"
    python3 "${REPORT_GENERATOR}" "${log_file}"
done



for existing_log in ./test_*-FVTR.log; do
    # Skip the combined summary log to prevent self-inclusion
    if [ "$(basename "${existing_log}")" = "test_all_modules-FVTR.log" ]; then
        continue
    fi
    if [ -f "${existing_log}" ]; then
        already_added="false"
        for collected_log in "${ALL_LOG_FILES[@]}"; do
            if [ "${collected_log}" = "${existing_log}" ]; then
                already_added="true"
                break
            fi
        done

        if [ "${already_added}" = "false" ]; then
            echo "Including existing FVTR log in combined summary: ${existing_log}"
            ALL_LOG_FILES+=("${existing_log}")
        fi
    fi
done




combined_log_file="./test_all_modules-FVTR.log"
: > "${combined_log_file}"
for log_file in "${ALL_LOG_FILES[@]}"; do
    cat "${log_file}" >> "${combined_log_file}"
    printf '\n' >> "${combined_log_file}"
done

echo "Generating cumulative HTML summary from ${combined_log_file}"
python3 "${REPORT_GENERATOR}" "${combined_log_file}"

echo ""
echo "=========================================="
echo "Deployment and Integration Execution Complete!"
echo "=========================================="
echo ""
echo "vHMC IP: ${HMC_IP}"
echo "Asterix managed system: ${ASTERIX_NAME}"
echo "Integration tests executed in sequence for all configured modules."
echo ""

