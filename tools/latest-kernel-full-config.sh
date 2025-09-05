#!/usr/bin/env bash

set -e -o pipefail

# Common error handling
bail() {
    if [[ $# -gt 0 ]]; then
        >&2 echo "Error: $*"
    fi
    exit 1
}

# Function to display usage information
usage() {
    cat << EOF
Usage: $(basename "$0") -r RPM_FILE

Extract kernel configurations from an RPM, merge with Bottlerocket's, and write out to a file, per architecture (x86_64 and aarch64).

This script provides functions intended to be run inside the bottlerocket-sdk container.
For typical usage, run 'make full-config RPM_FILE=/path/to/kernel-source.rpm' from the top-level bottlerocket-kernel-kit directory.

Direct script usage (for development/debugging):
    -r RPM_FILE    Path to RPM file
    -h             Display this help message

Dependencies (when running directly):
    - docker
    - rpm2cpio
    - cpio
    - tar
    - tq

Note: The inner_full_config() function is designed to run within the bottlerocket-sdk container environment
and expects specific mount points and toolchain paths to be available.
EOF
}

usage_error() {
    >&2 usage
    bail "$1"
}

check_dependencies() {
    hash rpm2cpio || usage_error "DEPENDENCY ERROR: Please install rpm2cpio somewhere in your PATH"
    hash cpio || usage_error "DEPENDENCY ERROR: Please install cpio somewhere in your PATH"
    hash tar || usage_error "DEPENDENCY ERROR: Please install tar somewhere in your PATH"
    hash docker || usage_error "DEPENDENCY ERROR: Please install docker somewhere in your PATH"
    hash tq || usage_error "DEPENDENCY ERROR: Please cargo install tomlq somewhere in your PATH"
}

# Get the SDK version from workspace Twoliter.toml and Twoliter.override, if provided.
# Assumes running in the top level of the project and `tq` on $PATH.
resolve_bottlerocket_sdk() {
    # Inspect Twoliter.lock file for [sdk] section source
    source="$(tq -r ".sdk.source" -f Twoliter.lock)"
    version="$(tq -r ".sdk.version" -f Twoliter.lock)"
    _name="$(tq -r ".sdk.name" -f Twoliter.lock)"
    # Trim from last slash, e.g. public.ecr.aws/bottlerocket/bottlerocket-sdk:v0.61.0 -> public.ecr.aws/bottlerocket
    _registry="${source%/*}"

    # Check Twoliter.override to get the registry. For simplicity, assume overrides are only against named project named bottlerocket-sdk
    registry="$(tq -r ".bottlerocket.bottlerocket-sdk.registry" -f Twoliter.override 2>/dev/null || echo "$_registry")"
    name="$(tq -r ".bottlerocket.bottlerocket-sdk.name" -f Twoliter.override 2>/dev/null || echo "$_name")"

    # Form the final SDK
    echo "${registry}/${name}:v${version}"
}

# Function to perform the kernel configuration merging inside Docker container
inner_full_config() {
    pushd /work || bail "Unable to enter /work (bind mount to tmp dir)"

    version="$(rpm --query --nosignature --queryformat '%{VERSION}' kernel-source.rpm)"
    majorminor=${version%.*} # Trim after last '.', e.g. 6.1.132 -> 6.1
    if [ "${majorminor}" == "6.12" ]; then
        # kernel 6.12 has a patch file that is not applied to the kernel sources, so
        # only pick up the 1000-series kernel patches for our purposes here.
        readarray -t br_patches < <(find /kernel-package -maxdepth 1 -name "10*.patch")
        spec_file="kernel6.12.spec"
        microcode_file="config-microcode-6-12"
    else
        readarray -t br_patches < <(find /kernel-package -maxdepth 1 -name "*.patch")
        spec_file="kernel.spec"
        microcode_file="config-microcode"
    fi

    kernel_path=/kernel-package

    rpm2cpio kernel-source.rpm | cpio -iu {,./}linux-"${version}".tar{,.xz} {,./}config-x86_64 {,./}config-aarch64 {,./}"*.patch" {,./}"${spec_file}"

    # Upstream source is either xz compressed tarball or plain tarball
    if [ -f "./linux-${version}.tar" ]; then
        tar -xof linux-"${version}".tar; rm linux-"${version}".tar
    else
        tar -xof linux-"${version}".tar.xz; rm linux-"${version}".tar.xz
    fi

    # Find upstream patch ordering based on the upstream SRPM so we can apply in that order
    readarray -t patches < <(grep -P "^Patch\d+" "${spec_file}" | sort -n -k1.6 | grep -oP "^Patch\d+: \K.*\.patch$" "${spec_file}")

    # Enter the source directory extracted from the tarball and patch
    pushd "linux-${version}" || bail "Could not move into linux-${version}"

    # Patches from the upstream
    for patch in "${patches[@]}"; do
        patch -p1 <"../$patch"
    done

    # Patches from bottlerocket
    for patch in "${br_patches[@]}"; do
        echo "Applying bottlerocket patch ${patch}"
        patch -p1 <"$patch"
    done

    popd || bail "Could not move around - 'popd' back to /work failed. Lets stop before we break anything further."

    for arch in "x86_64" "aarch64"; do

        export CROSS_COMPILE=/usr/bin/${arch}-bottlerocket-linux-gnu-
        export KCONFIG_CONFIG=bottlerocket_${arch}_defconfig

        br_cfg="${kernel_path}/config-bottlerocket"
        br_cfg_arch="${kernel_path}/config-bottlerocket-${arch}"
        microcode_cfg="/microcode/${microcode_file}"

        pushd "linux-${version}" || bail "Could not move into linux-${version}"

        if [ "${arch}" = "aarch64" ]; then
            karch="arm64"
            script_args=("../config-${arch}" "${br_cfg}" "${br_cfg_arch}")
        elif [ "${arch}" = "x86_64" ]; then
            karch="x86"
            script_args=("../config-${arch}" "${microcode_cfg}" "${br_cfg}" "${br_cfg_arch}")
        fi

        ARCH=${karch} ./scripts/kconfig/merge_config.sh "${script_args[@]}"
        mv -f "bottlerocket_${arch}_defconfig" "${kernel_path}/config-full-bottlerocket-${arch}" || bail "Failed to create config-full-bottlerocket-${arch}"
        popd || bail "Could not move around - 'popd' failed in merge_config loop. Lets stop before we break anything further."
    done

    # Validate the generated configurations
    echo "Validating generated kernel configurations..."
    validate_kernel_configs
}

# Function to validate kernel configurations (similar to validate_config.sh)
validate_kernel_configs() {
    local success=true
    local kernel_path=/kernel-package
    
    # Extract kernel version for validation
    local version
    version="$(rpm --query --nosignature --queryformat '%{VERSION}' kernel-source.rpm)"
    local majorminor=${version%.*} # Trim after last '.', e.g. 6.1.132 -> 6.1
    
    for arch in x86_64 aarch64; do
        echo "=== Validating kernel-${majorminor} ${arch} ==="
        
        # Check if files exist
        if [[ ! -f "${kernel_path}/config-bottlerocket" ]]; then
            echo "❌ Missing config-bottlerocket"
            success=false
            continue
        fi
        if [[ ! -f "${kernel_path}/config-bottlerocket-${arch}" ]]; then
            echo "❌ Missing config-bottlerocket-${arch}"
            success=false
            continue
        fi
        if [[ ! -f "${kernel_path}/config-full-bottlerocket-${arch}" ]]; then
            echo "❌ Missing config-full-bottlerocket-${arch}"
            success=false
            continue
        fi
        
        # Extract config lines (ignoring comments by default to avoid issues with removed kernel options)
        local common_configs
        common_configs=$(grep "^CONFIG_" "${kernel_path}/config-bottlerocket" | sort)
        local arch_configs
        arch_configs=$(grep "^CONFIG_" "${kernel_path}/config-bottlerocket-${arch}" | sort)
        local full_configs
        full_configs=$(grep "^CONFIG_" "${kernel_path}/config-full-bottlerocket-${arch}" | sort)
        
        # Check common configs
        local 
        missing_common=$(comm -23 <(echo "$common_configs") <(echo "$full_configs"))
        # Check arch-specific configs  
        local missing_arch
        missing_arch=$(comm -23 <(echo "$arch_configs") <(echo "$full_configs"))
        
        if [[ -n "$missing_common" ]]; then
            echo "❌ Missing common configs:"
            echo "$missing_common"
            success=false
        fi
        
        if [[ -n "$missing_arch" ]]; then
            echo "❌ Missing arch-specific configs:"
            echo "$missing_arch"
            success=false
        fi
        
        if [[ -z "$missing_common" && -z "$missing_arch" ]]; then
            echo "✅ All configs present for ${arch}"
        fi
    done
    
    if $success; then
        echo -e "\n🎉 All kernel config validations passed!"
    else
        echo -e "\n💥 Some kernel config validations failed!"
        bail "Kernel configuration validation failed"
    fi
}
