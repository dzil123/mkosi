# SPDX-License-Identifier: LGPL-2.1+

import contextlib
import errno
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from mkosi.config import ConfigFeature
from mkosi.log import ARG_DEBUG, die
from mkosi.run import run
from mkosi.sandbox import Mount, SandboxProtocol, nosandbox
from mkosi.types import PathString
from mkosi.versioncomp import GenericVersion


def statfs(path: Path, *, sandbox: SandboxProtocol = nosandbox) -> str:
    return run(
        ["stat", "--file-system", "--format", "%T", path],
        stdout=subprocess.PIPE,
        sandbox=sandbox(binary="stat", mounts=[Mount(path, path, ro=True)]),
    ).stdout.strip()


def is_subvolume(path: Path, *, sandbox: SandboxProtocol = nosandbox) -> bool:
    return path.is_dir() and path.stat().st_ino == 256 and statfs(path, sandbox=sandbox) == "btrfs"


def cp_version(*, sandbox: SandboxProtocol = nosandbox) -> GenericVersion:
    return GenericVersion(
        run(
            ["cp", "--version"],
            sandbox=sandbox(binary="cp"),
            stdout=subprocess.PIPE,
        ).stdout.splitlines()[0].split()[3]
    )


def make_tree(
    path: Path,
    *,
    use_subvolumes: ConfigFeature = ConfigFeature.disabled,
    sandbox: SandboxProtocol = nosandbox,
) -> Path:
    if statfs(path.parent, sandbox=sandbox) != "btrfs":
        if use_subvolumes == ConfigFeature.enabled:
            die(f"Subvolumes requested but {path} is not located on a btrfs filesystem")

        path.mkdir()
        return path

    if use_subvolumes != ConfigFeature.disabled:
        result = run(["btrfs", "subvolume", "create", path],
                     sandbox=sandbox(binary="btrfs", mounts=[Mount(path.parent, path.parent)]),
                     check=use_subvolumes == ConfigFeature.enabled).returncode
    else:
        result = 1

    if result != 0:
        path.mkdir()

    return path


@contextlib.contextmanager
def preserve_target_directories_stat(src: Path, dst: Path) -> Iterator[None]:
    dirs = [p for d in src.glob("**/") if (dst / (p := d.relative_to(src))).exists()]

    with tempfile.TemporaryDirectory() as tmp:
        for d in dirs:
            (tmp / d).mkdir(exist_ok=True)
            shutil.copystat(dst / d, tmp / d)

        yield

        for d in dirs:
            shutil.copystat(tmp / d, dst / d)


def copy_tree(
    src: Path,
    dst: Path,
    *,
    preserve: bool = True,
    dereference: bool = False,
    use_subvolumes: ConfigFeature = ConfigFeature.disabled,
    sandbox: SandboxProtocol = nosandbox,
) -> Path:
    copy: list[PathString] = [
        "cp",
        "--recursive",
        "--dereference" if dereference else "--no-dereference",
        f"--preserve=mode,links{',timestamps,ownership,xattr' if preserve else ''}",
        "--reflink=auto",
        "--copy-contents",
        src, dst,
    ]
    if cp_version(sandbox=sandbox) >= "9.5":
        copy += ["--keep-directory-symlink"]

    mounts = [Mount(src, src, ro=True), Mount(dst.parent, dst.parent)]

    # If the source and destination are both directories, we want to merge the source directory with the
    # destination directory. If the source if a file and the destination is a directory, we want to copy
    # the source inside the directory.
    if src.is_dir():
        copy += ["--no-target-directory"]

    # Subvolumes always have inode 256 so we can use that to check if a directory is a subvolume.
    if (
        use_subvolumes == ConfigFeature.disabled or
        not preserve or
        not is_subvolume(src, sandbox=sandbox) or
        (dst.exists() and any(dst.iterdir()))
    ):
        with (
            preserve_target_directories_stat(src, dst)
            if not preserve
            else contextlib.nullcontext()
        ):
            run(copy, sandbox=sandbox(binary="cp", mounts=mounts))
        return dst

    # btrfs can't snapshot to an existing directory so make sure the destination does not exist.
    if dst.exists():
        dst.rmdir()

    result = run(
        ["btrfs", "subvolume", "snapshot", src, dst],
        check=use_subvolumes == ConfigFeature.enabled,
        sandbox=sandbox(binary="btrfs", mounts=mounts),
    ).returncode

    if result != 0:
        with (
            preserve_target_directories_stat(src, dst)
            if not preserve
            else contextlib.nullcontext()
        ):
            run(copy, sandbox=sandbox(binary="cp", mounts=mounts))

    return dst


def rmtree(*paths: Path, sandbox: SandboxProtocol = nosandbox) -> None:
    if not paths:
        return

    if subvolumes := sorted({p for p in paths if is_subvolume(p, sandbox=sandbox)}):
        # Silence and ignore failures since when not running as root, this will fail with a permission error unless the
        # btrfs filesystem is mounted with user_subvol_rm_allowed.
        run(["btrfs", "subvolume", "delete", *subvolumes],
            check=False,
            sandbox=sandbox(binary="btrfs", mounts=[Mount(p.parent, p.parent) for p in subvolumes]),
            stdout=subprocess.DEVNULL if not ARG_DEBUG.get() else None,
            stderr=subprocess.DEVNULL if not ARG_DEBUG.get() else None)

    filtered = sorted({p for p in paths if p.exists()})
    if filtered:
        run(["ls", "-lAh", "--", *filtered],
            sandbox=sandbox(binary="ls", mounts=[Mount(p.parent, p.parent) for p in filtered]))
        # run(["find", *filtered],
        #     sandbox=sandbox(binary="find", mounts=[Mount(p.parent, p.parent) for p in filtered]))
        # run(["rm", "-rdvf", "--", *filtered],
        #     sandbox=sandbox(binary="rm", mounts=[Mount(p.parent, p.parent) for p in filtered]))
        # for d in filtered:
        #     shutil.rmtree(d, ignore_errors=True)
        run(["rm", "-rf", "--", *filtered],
            sandbox=sandbox(binary="rm", mounts=[Mount(p.parent, p.parent) for p in filtered]))


def move_tree(
    src: Path,
    dst: Path,
    *,
    use_subvolumes: ConfigFeature = ConfigFeature.disabled,
    sandbox: SandboxProtocol = nosandbox
) -> Path:
    if src == dst:
        return dst

    if dst.is_dir():
        dst = dst / src.name

    try:
        src.rename(dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise e

        logging.info(
            f"Could not rename {src} to {dst} as they are located on different devices, falling back to copying"
        )
        copy_tree(src, dst, use_subvolumes=use_subvolumes, sandbox=sandbox)
        rmtree(src, sandbox=sandbox)

    return dst
