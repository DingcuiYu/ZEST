# ZEST

ZEST is a patch set for experimenting with swap on Google's GKI common
kernel. The instructions below use the Android 15, 6.6 kernel tree listed in
the build commands.

## Patch sets

The patches in `patches/` are separate patch sets for different experiments.
Apply the patch set that matches the experiment to a clean checkout of the
base kernel. They are all based on the same GKI source revision; do not stack
alternative patch sets unless you have checked that they apply cleanly and
that their interfaces are compatible.

| Patch | Purpose |
| --- | --- |
| [`patches/zest.patch`](patches/zest.patch) | The complete ZEST source code. Use this patch for both zram-swap and ordinary block-swap experiments. |
| [`patches/b_32k.patch`](patches/b_32k.patch) | Enables 32 KiB chunk swap groups for block swap. |
| [`patches/trace.patch`](patches/trace.patch) | Adds the swap-slot/lifecycle trace instrumentation used for swap trace collection. |

For example:

```bash
git apply patches/zest.patch
```

Replace `zest.patch` with `b_32k.patch` or `trace.patch` when that is the
patch set required by the experiment.

## Get the base kernel

Initialize the Android common-kernel manifest:

```bash
repo init -u https://android.googlesource.com/kernel/manifest \
  -b common-android15-6.6-2025-1
```

Download the build manifest from
<https://ci.android.com/builds/submitted/14739656/kernel_aarch64/latest/manifest_14739656.xml>,
then select it:

```bash
repo init -m manifest_14739656.xml
repo sync -j "$(nproc)"
```

Apply the required patch set as described above.

## Build

The device driver source is not available in this repository. Therefore only
the GKI `boot.img` can be built; a complete vendor/device image cannot be
reproduced here.

After applying a patch, the kernel version must not be generated with a
`-dirty` suffix. The suffix can cause boot or module verification failures.
Before building:

1. Add every new file introduced by the patch (and any local generated file)
   to `.gitignore`. Check `git status` so that no required source file is
   accidentally ignored.
2. Mark all current tracked changes as assumed unchanged in one operation:

   ```bash
   git diff --name-only -z | xargs -0 -r git update-index --assume-unchanged --
   ```

Confirm that `git status` is clean before invoking the build. Build the arm64
kernel and place its artifacts in `out/dist`:

```bash
BUILD_NUMBER="14739656" tools/bazel run //common:kernel_aarch64_abi_dist \
  --config=stamp -- --dist_dir=out/dist
```

## Flash the boot image

Once the build completes, flash only the resulting `boot.img`:

```bash
adb reboot bootloader
fastboot flash boot boot.img
fastboot reboot
```

## Enable ZEST

Push the helper before enabling ZEST. The path to the swap file may need to be
adapted for the target device.

```bash
adb push pin /data/local/tmp/pin
adb shell chmod 755 /data/local/tmp/pin
```

The following example uses `dm-53`; replace it with the actual F2FS device
path. It enables the 32 KiB ZEST chunk-group layout and resets the swap-fault
counters before the run.

```bash
#!/system/bin/sh
echo 1 > /sys/fs/f2fs/dm-53/zest_enable
echo 100 > /sys/fs/f2fs/dm-53/zest_op_ratio
echo 1 > /sys/kernel/zest/layout_mode
swapoff /dev/block/zram0
echo 1 > /sys/kernel/zest/swap_fault_stats_reset
rm -rf /data/swapfile
touch /data/swapfile
/data/local/tmp/pin /data/swapfile
fallocate -l 7974620k /data/swapfile
chmod 600 /data/swapfile
mkswap /data/swapfile
swapon /data/swapfile
dmesg > /data/local/tmp/zest_dmesg.txt
```

## Enable ordinary block swap

This is the block-swap setup used with `zest.patch`. It does not use the
`pin` helper:

```bash
#!/system/bin/sh
swapoff /dev/block/zram0
echo 1 > /sys/kernel/zest/swap_fault_stats_reset
rm -rf /data/swapfile-block
touch /data/swapfile-block
fallocate -l 7974620k /data/swapfile-block
chmod 600 /data/swapfile-block
mkswap /data/swapfile-block
swapon /data/swapfile-block
```

## Swap latency trace

Swap-latency tracing is provided by `zest.patch`. It allocates a large
in-kernel memory area and therefore has non-negligible impact on system
activity. Do not enable it for end-to-end performance measurements; use it for
debugging and path analysis only.

Use the debugfs interface directly, in the same style as the slot-trace
workflow. This documents the kernel controls behind the Python helper
workflow without embedding the helper function bodies:

1. Mount debugfs and inspect the interface once after boot:

   ```bash
   adb root
   adb shell
   mkdir -p /sys/kernel/debug
   mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
   cd /sys/kernel/debug/swap_latency
   ls
   # enable  buffer_size_mb  reset  log  format
   cat format
   ```

2. Optionally choose the buffer size while no session is active. The default
   is 2048 MiB and the supported range is 1--32768 MiB. If a previous
   allocation prevents changing the size, release it first with `echo 1 > reset`.

   ```bash
   echo 2048 > buffer_size_mb
   ```

3. Start a fresh trace session. Writing `1` to `enable` clears the previous
   session contents and allocates or reuses the buffer:

   ```bash
   echo 1 > enable
   ```

4. Run the workload. Do not use this trace for end-to-end performance numbers
   because the allocated buffer and instrumentation perturb system activity.

5. Stop and drain the session, then export the log from the device:

   ```bash
   echo 0 > enable
   cat log > /data/local/tmp/swap_latency.bin
   exit
   ```

6. On the host, pull and analyze the captured file with the parser added by
   the patch:

   ```bash
   adb pull /data/local/tmp/swap_latency.bin
   python3 tools/testing/swap_latency/analyze_swap_latency_paths.py \
     swap_latency.bin
   ```

The trace controls are under `/sys/kernel/debug/swap_latency/`. The `log`
file is readable only after tracing has stopped (`enable=0`). Repeat steps 3--6
for additional sessions; each new `echo 1 > enable` starts with an empty
buffer.

## Swap-slot trace

Apply `patches/trace.patch` for swap-slot/lifecycle trace collection. The
complete command sequence is documented in [`slot_trace.txt`](slot_trace.txt):

1. Mount debugfs and enter `/sys/kernel/debug/swap_lifecycle_trace/`.
2. Optionally set `buffer_size_mb` while no session is active.
3. Write `1` to `enable` and run the workload.
4. Write `0` to `enable` before reading `log`.
5. Export the binary log and analyze it with
   `tools/testing/swap_lifecycle/analyze_swap_lifecycle.py` from the patched
   kernel tree.

## Supplementary data for the paper

The current ZEST code supports only the 32 KiB Chunk Group. Other chunk sizes
were not kept because their results were less favorable. The historical data
below are provided for reference only; the experimental conditions match the
motivation experiment in the paper.

|  | 32K | 64K | 128K | 256K |
| --- |---: | ---: | ---: | ---: |
| Average startup time | 307.2 ms |  407.9 ms | 449.05 ms | 430 ms |
| WAF |  1.062 | 1.110 | 1.087 | 1.048 |
| Cache hit ratio |  80.7% | 89.9% | 94.0% | 96.7% |

The baseline column has no WAF or cache-hit-ratio value because those metrics
were reported only for the four chunk-group sizes.
