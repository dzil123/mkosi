# SPDX-License-Identifier: LGPL-2.1+

from pathlib import Path

from mkosi.distributions import centos
from mkosi.installer.dnf import Repo
from mkosi.state import MkosiState


class Installer(centos.Installer):
    @classmethod
    def pretty_name(cls) -> str:
        return "Rocky Linux"

    @staticmethod
    def gpgurls(state: MkosiState) -> tuple[str, ...]:
        gpgpath = Path(f"/usr/share/distribution-gpg-keys/rocky/RPM-GPG-KEY-Rocky-{state.config.release}")
        if gpgpath.exists():
            return (f"file://{gpgpath}",)
        else:
            return ("https://download.rockylinux.org/pub/rocky/RPM-GPG-KEY-Rocky-$releasever",)

    @classmethod
    def repository_variants(cls, state: MkosiState, repo: str) -> list[Repo]:
        if state.config.mirror:
            url = f"baseurl={state.config.mirror}/rocky/$releasever/{repo}/$basearch/os"
        else:
            url = f"mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo={repo}-$releasever"

        return [Repo(repo, url, cls.gpgurls(state))]

    @classmethod
    def sig_repositories(cls, state: MkosiState) -> list[Repo]:
        return []
