This is the zest patch set relative to Google's GKI common kernel.

## Usage

Download the base source code:

```
repo init -u https://android.googlesource.com/kernel/manifest -b common-android15-6.6-2025-1
```

Download https://ci.android.com/builds/submitted/14739656/kernel_aarch64/latest/manifest_14739656.xml, then run:

```
repo init -m manifest_14739656.xml
```

Sync the source code:

```
repo sync -j `nproc`
```

Now you can apply the patch.

## Build

```
BUILD_NUMBER="14739656" tools/bazel run //common:kernel_aarch64_abi_dist --config=stamp -- --dist_dir=out/dist
```

## Enable zest

Note: replace `dm-53` with your actual device path as needed.

```bash
#!/system/bin/sh
echo 1 > /sys/fs/f2fs/dm-53/zest_enable
swapoff /dev/block/zram0
rm -rf /data/swapfile
touch /data/swapfile
/data/local/tmp/pin /data/swapfile
fallocate -l 7974620k /data/swapfile
chmod 600 /data/swapfile
mkswap /data/swapfile
swapon /data/swapfile
dmesg >  /data/local/tmp/zest_dmesg.txt
```

## Notes

**1. Driver verification bypass**

I was unable to find open-source driver code corresponding to the Google Pixel 10 Pro, so only the GKI kernel source for the matching version was downloaded. This causes driver verification to fail during loading. To work around this check, the following files were modified:

```c
// common/kernel/module/gki_module.c
bool gki_is_module_protected_export(const char *name)
{
    return false;
    ...
}
```

```c
// common/kernel/module/version.c
int check_version(const struct load_info *info,
                  const char *symname,
                  struct module *mod,
                  const s32 *crc)
{
    return 1;
    ...
}
```

Finally, instruct git to ignore all modifications under the `common` directory:

```
git update-index --assume-unchanged [all modified files]
```

Verify that `git status` shows a clean state.

**2. Experimental read optimizations**

The provided patch retains some read optimization experiments I attempted (read cache, reorder, etc.). Unfortunately, they are not mature enough and introduce side effects, so they are included here for reference only.