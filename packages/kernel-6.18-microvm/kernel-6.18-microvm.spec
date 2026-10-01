%global debug_package %{nil}
%global __strip /bin/true

%global kmajor 6.18

Name: %{_cross_os}kernel-%{kmajor}-microvm
Version: 6.18.51
Release: 1%{?dist}
Summary: The Linux kernel, configured for microvm guests
License: GPL-2.0 WITH Linux-syscall-note
URL: https://www.kernel.org/
# Keep this in sync with packages/kernel-6.18 so both kernels are built from the
# same source. Use that package's latest-kernel-srpm-url.sh to get this.
Source0: https://cdn.amazonlinux.com/al2023/blobstore/efbc735c0fd0c62409de4680a84db7d26a02b2f40a9a7c4a30037ed78b689c21/kernel6.18-6.18.51-120.162.amzn2023.src.rpm
Source1: gpgkey-B21C50FA44A99720EAA72F7FE951904AD832C631.asc

# Custom Bottlerocket kernel configurations, shared with packages/kernel-6.18.
Source100: config-bottlerocket
Source101: config-bottlerocket-x86_64
Source102: config-bottlerocket-aarch64
# Overrides that make this kernel bootable under Firecracker.
Source103: config-microvm

# Adjust kernel-devel mount behavior if not squashfs.
Source210: var-lib-kernel-devel-lower.mount.drop-in.conf.in

# Bootconfig snippet to adjust the default kernel command line for the platform.
Source300: bootconfig-aws.conf

# Help out-of-tree module builds run `make prepare` automatically.
Patch1001: 1001-Makefile-add-prepare-target-for-external-modules.patch
# Expose tools/* targets for out-of-tree module builds.
Patch1002: 1002-Revert-kbuild-hide-tools-build-targets-from-external.patch
# Enable INITRAMFS_FORCE config option for our use case.
Patch1003: 1003-initramfs-unlink-INITRAMFS_FORCE-from-CMDLINE_-EXTEN.patch
# Increase default of sysctl net.unix.max_dgram_qlen to 512.
Patch1004: 1004-af_unix-increase-default-max_dgram_qlen-to-512.patch
# Select prerequisites for GPU drivers.
Patch1005: 1005-drm-simpledrm-Select-prerequisites-for-gpu-drivers.patch
Patch1006: 1006-Revert-selinux-fix-overlayfs-mmap-and-mprotect-acces.patch
# Backport patch to disable LZ4 rolling decompression on EROFS
Patch1007: 1007-erofs-disable-LZ4-rolling-decompression-for-now.patch
# Reserve blocks for XFS parent pointer updates to avoid a filesystem shutdown
Patch1008: 1008-xfs-initialise-args-total-for-parent-pointer-updates.patch

BuildRequires: bc
BuildRequires: elfutils-devel
BuildRequires: hostname
BuildRequires: kmod
BuildRequires: openssl-devel

# CPU microcode updates are included as "extra firmware" so the files don't
# need to be installed on the root filesystem. However, we want the license and
# attribution files to be available in the usual place. x86_64 only.
%if "%{_cross_arch}" == "x86_64"
BuildRequires: %{_cross_os}microcode-ec2
Requires: %{_cross_os}microcode-licenses
%endif

# No bare-metal for this kernel
Conflicts: %{_cross_os}variant-platform(metal)

# No squashfs support, rely on erofs for compression
Conflicts: %{_cross_os}image-feature(no-erofs-root-partition)

# No runtime kernel-devel support
Conflicts: %{_cross_os}image-feature(external-kmod-development)

# Legacy iptables support is not enabled in this kernel.
Conflicts: %{_cross_os}iptables-legacy

# Pull in expected modules.
Requires: %{name}-modules = %{version}-%{release}

# Pull in default mkfs conf for xfsprogs.
Requires: (%{name}-mkfs-xfs-conf if %{_cross_os}xfsprogs)

# Pull in platform-dependent boot config snippets.
Requires: (%{name}-bootconfig-aws if %{_cross_os}variant-platform(aws))

%global _cross_ksrcdir %{_cross_usrsrc}/kernels/%{version}
%global _cross_kmoddir %{_cross_libdir}/modules/%{version}

%description
%{summary}.

%package devel
Summary: Configured Linux kernel source for module building

%description devel
%{summary}.

%package bootconfig-aws
Summary: Boot config snippet for the Linux kernel on AWS

%description bootconfig-aws
%{summary}.

%package modules
Summary: Modules for the Linux kernel

%description modules
%{summary}.

%package mkfs-xfs-conf
Summary: mkfs configurations for the XFS filesystem

%description mkfs-xfs-conf
%{summary}.

%package headers
Summary: Header files for the Linux kernel for use by glibc

%description headers
%{summary}.

%prep
%if "%{_cross_arch}" == "aarch64"
%global _cross_kimage vmlinuz.efi
%endif

%global _ko ko

rpmkeys --import %{S:1} --dbpath "${PWD}/rpmdb"
rpmkeys --checksig %{S:0} --dbpath "${PWD}/rpmdb"
rm -rf "${PWD}/rpmdb"
rpm2cpio %{S:0} | cpio -iu {,./}linux-%{version}.tar.xz {,./}config-%{_cross_arch} {,./}"*.patch" {,./}kernel6.18.spec
tar -xof linux-%{version}.tar.xz; rm linux-%{version}.tar.xz
# Count all the patches extracted from the SRPM
patches_count=$(find -name "*.patch" | wc -l)
# Find patch ordering based on the Source0 kernel.spec file from the SRPM.
# First, find all `PatchNNN` lines. Then, sort by the patch number (-k1.6 in sort sets the 6th char
# in field 1 of input as the sort parameter). Finally, capture just the patch file name specified.
readarray -t patches < <(grep -P "^Patch\d+" kernel6.18.spec | sort -n -k1.6 | grep -oP "^Patch\d+: \K.*\.patch$" kernel6.18.spec)
# Fail the build if there is a mismatch in the number of patches found
if [[ "${patches_count}" -ne "${#patches[@]}" ]]; then
  echo "Mismatch on patches count!"
  exit 1
fi

%setup -TDn linux-%{version}
# Patches from the Source0 SRPM
for patch in ${patches[@]}; do
    patch -p1 <../"$patch"
done
# Patches listed in this spec (Patch0001...)
%autopatch -p1 -M 1999

%if "%{_cross_arch}" == "x86_64"
microcode="$(find %{_cross_libdir}/firmware -type f -path '*/*-ucode/*' -printf '%%P\n' | sort | tr '\n' ' ')"
cat <<EOF > ../config-microcode
CONFIG_EXTRA_FIRMWARE="${microcode}"
CONFIG_EXTRA_FIRMWARE_DIR="%{_cross_libdir}/firmware"
EOF
%endif

export ARCH="%{_cross_karch}"
export CROSS_COMPILE="%{_cross_target}-"

export KCONFIG_CONFIG="arch/%{_cross_karch}/configs/%{_cross_vendor}_defconfig"
scripts/kconfig/merge_config.sh \
  ../config-%{_cross_arch} \
%if "%{_cross_arch}" == "x86_64"
  ../config-microcode \
  %{S:101} \
%else
  %{S:102} \
%endif
  %{S:100} \
  %{S:103}

# Unlike packages/kernel-6.18, this flavor is not validated against a checked-in
# full kernel configuration. Assert instead that the options this kernel exists
# for actually survived the merge, since `olddefconfig` will silently demote a
# built-in option back to a module if one of its dependencies is modular. Any of
# these being modular would leave the guest unable to find its root filesystem
# or talk to the host, which is much harder to debug from a boot hang.
for opt in \
  CONFIG_VIRTIO \
  CONFIG_VIRTIO_MMIO \
  CONFIG_VIRTIO_BLK \
  CONFIG_VIRTIO_NET \
  CONFIG_VSOCKETS \
  CONFIG_VIRTIO_VSOCKETS \
  CONFIG_BLK_DEV_DM \
  CONFIG_DM_INIT \
  CONFIG_DM_VERITY \
  CONFIG_EROFS_FS \
  CONFIG_SERIAL_8250 \
  CONFIG_SERIAL_8250_CONSOLE \
; do
  if ! grep -qx "${opt}=y" "${KCONFIG_CONFIG}"; then
    echo "error: ${opt} must be built in for microvm guests, but is:"
    grep -E "^(# )?${opt}[= ]" "${KCONFIG_CONFIG}" || echo "  (absent)"
    exit 1
  fi
done

rm -f ../config-* ../*.patch
cd %{_builddir}

%global kmake %{shrink: \
make -s \
  ARCH="%{_cross_karch}" \
  CROSS_COMPILE="%{_cross_target}-" \
  INSTALL_HDR_PATH="%{buildroot}%{_cross_prefix}" \
  INSTALL_MOD_PATH="%{buildroot}%{_cross_prefix}" \
  INSTALL_MOD_STRIP=1 \
  %{nil}}

%build
%kmake mrproper
%kmake %{_cross_vendor}_defconfig
%kmake %{?_smp_mflags} %{_cross_kimage}
%kmake %{?_smp_mflags} modules

make -C tools/bpf/bpftool bootstrap
./tools/bpf/bpftool/bootstrap/bpftool btf dump file vmlinux format c > vmlinux.h

%install
%kmake %{?_smp_mflags} headers_install
%kmake %{?_smp_mflags} modules_install

install -d %{buildroot}/boot
install -T -m 0755 arch/%{_cross_karch}/boot/%{_cross_kimage} %{buildroot}/boot/%{_cross_kimage}
# Firecracker boots an uncompressed ELF kernel, and the EIF image format used by
# the enclave variant consumes this file directly, so ship it alongside the
# compressed image. Debug info is dropped because %%__strip is disabled for this
# package, and leaving DWARF in place makes the kernel image an order of
# magnitude larger for a file that gets loaded into guest memory as-is. The
# symbol table is kept so the image is still usable for debugging a boot.
install -T -m 0755 vmlinux %{buildroot}/boot/vmlinux
%{_cross_target}-objcopy --strip-debug %{buildroot}/boot/vmlinux
install -m 0644 .config %{buildroot}/boot/config

find %{buildroot}%{_cross_prefix} \
   \( -name .install -o -name .check -o \
      -name ..install.cmd -o -name ..check.cmd \) -delete

# For out-of-tree kmod builds, we need to support the following targets:
#   make scripts -> make prepare -> make modules
#
# This requires enough of the kernel tree to build host programs under the
# "scripts" and "tools" directories.

# Any existing ELF objects will not work properly if we're cross-compiling for
# a different architecture, so get rid of them to avoid confusing errors.
find arch scripts tools -type f -executable \
  -exec sh -c "head -c4 {} | grep -q ELF && rm {}" \;

# We don't need to include these files.
find -type f \( -name \*.cmd -o -name \*.gitignore \) -delete

# Avoid an OpenSSL dependency by stubbing out options for module signing and
# trusted keyrings, so `sign-file` and `extract-cert` won't be built. External
# kernel modules do not have access to the keys they would need to make use of
# these tools.
sed -i \
  -e 's,$(CONFIG_MODULE_SIG_FORMAT),n,g' \
  -e 's,$(CONFIG_SYSTEM_TRUSTED_KEYRING),n,g' \
  scripts/Makefile

(
  find * \
    -type f \
    \( -name Build\* -o -name Kbuild\* -o -name Kconfig\* -o -name Makefile\* \) \
    -print

  find arch/%{_cross_karch}/ \
    -type f \
    \( -name module.lds -o -name vmlinux.lds.S -o -name Platform -o -name \*.tbl \) \
    -print

  find arch/%{_cross_karch}/{include,lib}/ -type f ! -name \*.o ! -name \*.o.d ! -name \*.a -print
  echo arch/%{_cross_karch}/kernel/asm-offsets.s
  echo lib/vdso/gettimeofday.c

  for d in \
    arch/%{_cross_karch}/tools \
    arch/%{_cross_karch}/kernel/vdso ; do
    [ -d "${d}" ] && find "${d}/" -type f ! -name \*.o -print
  done

  find include -type f -print
  find scripts -type f ! -name \*.l ! -name \*.y ! -name \*.o -print

  find tools/{arch/%{_cross_karch},include,objtool,scripts}/ -type f ! -name \*.o ! -name \*.a -print
  echo tools/build/fixdep.c
  find tools/lib/subcmd -type f -print
  find tools/lib/{ctype,hweight,rbtree,string,str_error_r}.c

  echo kernel/bounds.c
  echo kernel/time/timeconst.bc
  echo security/selinux/include/classmap.h
  echo security/selinux/include/initial_sid_to_string.h
  echo security/selinux/include/policycap.h
  echo security/selinux/include/policycap_names.h

  echo .config
  echo Module.symvers
  echo System.map
  echo vmlinux.h
) | sort -u > kernel_devel_files

# Install development files into the canonical location for use by downstream
# packages as a build dependency.
install -d %{buildroot}%{_cross_ksrcdir}
tar c -T kernel_devel_files | tar x -C %{buildroot}%{_cross_ksrcdir}

# Replace the incorrect links from modules_install.
rm -f %{buildroot}%{_cross_kmoddir}/build %{buildroot}%{_cross_kmoddir}/source
ln -rs %{_cross_ksrcdir} %{buildroot}%{_cross_kmoddir}/build
ln -rs %{_cross_ksrcdir} %{buildroot}%{_cross_kmoddir}/source

# Make it easy to find sources and modules across minor version changes.
ln -rs %{buildroot}%{_cross_ksrcdir} %{buildroot}%{_cross_usrsrc}/kernels/%{kmajor}
ln -rs %{buildroot}%{_cross_kmoddir} %{buildroot}%{_cross_libdir}/modules/%{kmajor}

# Install a copy of System.map so that module dependencies can be regenerated.
install -p -m 0600 System.map %{buildroot}%{_cross_kmoddir}

# Create the mount point for the runtime kernel-devel directory, and populate
# with the linker script that driverdog needs.
install -d %{buildroot}%{_cross_datadir}/bottlerocket/kernel-devel/%{version}/scripts
install -p -m 0644 scripts/module.lds \
  %{buildroot}%{_cross_datadir}/bottlerocket/kernel-devel/%{version}/scripts

# Add a drop-in for compatibility with the release package's mount unit.
LOWERPATH=$(systemd-escape --path %{_cross_sharedstatedir}/kernel-devel/.overlay/lower)
mkdir -p %{buildroot}%{_cross_unitdir}/"${LOWERPATH}.mount.d"
sed -e 's|PREFIX|%{_cross_prefix}|g' %{S:210} \
  > %{buildroot}%{_cross_unitdir}/"${LOWERPATH}.mount.d"/no-squashfs.conf

# Add symlink for kernel 6.18 xfsprogs-mkfs defaults in the default path.
mkdir -p %{buildroot}%{_cross_datadir}/xfsprogs/mkfs
ln -s lts_6.18.conf %{buildroot}%{_cross_datadir}/xfsprogs/mkfs/default.conf

# Install platform-specific bootconfig snippets.
install -d %{buildroot}%{_cross_bootconfigdir}
install -p -m 0644 %{S:300} %{buildroot}%{_cross_bootconfigdir}/05-aws.conf

%files
%license COPYING LICENSES/preferred/GPL-2.0 LICENSES/exceptions/Linux-syscall-note
%{_cross_attribution_file}
/boot/%{_cross_kimage}
/boot/vmlinux
/boot/config
%dir %{_cross_usrsrc}/kernels
%dir %{_cross_datadir}/bottlerocket/kernel-devel
%{_cross_datadir}/bottlerocket/kernel-devel/*
%{_cross_unitdir}/*kernel*devel*.mount.d/no-squashfs.conf

%files mkfs-xfs-conf
%{_cross_datadir}/xfsprogs/mkfs/default.conf

%files headers
%dir %{_cross_includedir}/asm
%dir %{_cross_includedir}/asm-generic
%dir %{_cross_includedir}/cxl
%dir %{_cross_includedir}/drm
%dir %{_cross_includedir}/fwctl
%dir %{_cross_includedir}/linux
%dir %{_cross_includedir}/misc
%dir %{_cross_includedir}/mtd
%dir %{_cross_includedir}/rdma
%dir %{_cross_includedir}/regulator
%dir %{_cross_includedir}/scsi
%dir %{_cross_includedir}/sound
%dir %{_cross_includedir}/video
%dir %{_cross_includedir}/xen
%{_cross_includedir}/asm/*
%{_cross_includedir}/asm-generic/*
%{_cross_includedir}/cxl/*
%{_cross_includedir}/drm/*
%{_cross_includedir}/fwctl/*
%{_cross_includedir}/linux/*
%{_cross_includedir}/misc/*
%{_cross_includedir}/mtd/*
%{_cross_includedir}/rdma/*
%{_cross_includedir}/regulator/*
%{_cross_includedir}/scsi/*
%{_cross_includedir}/sound/*
%{_cross_includedir}/video/*
%{_cross_includedir}/xen/*

%files devel
# Allow downstream package builds to modify these files, since they need to
# rebuild tools for the current host architecture.
%defattr(664, root, builder, 775)
%{_cross_usrsrc}/kernels/%{kmajor}
%{_cross_ksrcdir}
%{_cross_kmoddir}/source
%{_cross_kmoddir}/build
%attr(775, root, builder) %{_cross_ksrcdir}/scripts/*

%files bootconfig-aws
%{_cross_bootconfigdir}/05-aws.conf

# Unlike packages/kernel-6.18, the modules are not enumerated individually here.
# This kernel builds almost everything in, so the surviving module set is both
# small and a side effect of config-microvm rather than a deliberate list, and
# enumerating it would only add a second place to update.
%files modules
%dir %{_cross_libdir}/modules
%{_cross_libdir}/modules/%{kmajor}
%dir %{_cross_kmoddir}
%{_cross_kmoddir}/modules.*
%{_cross_kmoddir}/System.map
%{_cross_kmoddir}/kernel

%changelog
