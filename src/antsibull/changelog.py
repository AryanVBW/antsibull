# Author: Felix Fontein <felix@fontein.de>
# Author: Toshio Kuratomi <tkuratom@redhat.com>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Ansible Project, 2020
"""Changelog handling and processing code."""

from __future__ import annotations

import asyncio
import datetime
import glob
import os
import os.path
import tarfile
import tempfile
import typing as t
from collections import defaultdict

import aiohttp
import asyncio_pool  # type: ignore[import]
from antsibull_changelog.changes import ChangesData, add_release
from antsibull_changelog.config import (
    ChangelogConfig,
    CollectionDetails,
    PathsConfig,
    TextFormat,
)
from antsibull_changelog.fragment import ChangelogFragment
from antsibull_changelog.rendering.changelog import ChangelogGenerator
from antsibull_changelog.utils import collect_versions
from antsibull_core import app_context
from antsibull_core.ansible_core import get_ansible_core
from antsibull_core.dependency_files import DependencyFileData, DepsFile
from antsibull_core.galaxy import CollectionDownloader, GalaxyContext
from antsibull_core.logging import log
from antsibull_core.schemas.collection_meta import (
    CollectionsMetadata,
    RemovalInformation,
    RemovedRemovalInformation,
)
from antsibull_docs_parser.parser import Context as _AnsibleMarkupContext
from antsibull_docs_parser.parser import Whitespace as _AnsibleMarkupWhitespace
from antsibull_docs_parser.parser import parse as _parse_ansible_markup
from antsibull_docs_parser.rst import to_rst_plain as _ansible_markup_to_rst
from antsibull_fileutils.yaml import load_yaml_bytes
from packaging.version import Version as PypiVer
from semantic_version import Version as SemVer

mlog = log.fields(mod=__name__)


class ChangelogData:
    """
    Data for a single changelog (for a collection, for ansible-core, for Ansible)
    """

    paths: PathsConfig
    config: ChangelogConfig
    changes: ChangesData
    generator: ChangelogGenerator
    generator_flatmap: bool

    def __init__(
        self,
        paths: PathsConfig,
        config: ChangelogConfig,
        changes: ChangesData,
        flatmap: bool = False,
    ):
        self.paths = paths
        self.config = config
        self.changes = changes
        self.generator_flatmap = flatmap
        self.generator = ChangelogGenerator(
            self.config, self.changes, plugins=None, fragments=None, flatmap=flatmap
        )

    @classmethod
    def collection(
        cls, collection_name: str, version: str, changelog_data: t.Any | None = None
    ) -> ChangelogData:
        paths = PathsConfig.force_collection("")
        collection_details = CollectionDetails(paths)
        collection_details.namespace, collection_details.name = collection_name.split(
            ".", 1
        )
        collection_details.version = version
        collection_details.flatmap = False  # TODO!
        config = ChangelogConfig.default(paths, collection_details)
        return cls(
            paths, config, ChangesData(config, "", changelog_data), flatmap=True
        )  # TODO!

    @classmethod
    def ansible_core(cls, changelog_data: t.Any | None = None) -> ChangelogData:
        paths = PathsConfig.force_ansible("")
        collection_details = CollectionDetails(paths)
        config = ChangelogConfig.default(paths, collection_details)
        return cls(
            paths, config, ChangesData(config, "", changelog_data), flatmap=False
        )

    @classmethod
    def ansible(
        cls, directory: str | None, output_directory: str | None = None
    ) -> ChangelogData:
        paths = PathsConfig.force_ansible("")

        config = ChangelogConfig.default(paths, CollectionDetails(paths), "Ansible")
        # TODO: adjust the following lines once Ansible switches to semantic versioning
        config.use_semantic_versioning = False
        config.release_tag_re = r"""(v(?:[\d.ab\-]|rc)+)"""
        config.pre_release_tag_re = r"""(?P<pre_release>(?:[ab]|rc)+\d*)$"""

        changelog_path = ""
        if directory is not None:
            changelog_path = os.path.join(directory, "changelog.yaml")
        changes = ChangesData(config, changelog_path)
        if output_directory is not None:
            changes.path = os.path.join(output_directory, "changelog.yaml")
        return cls(paths, config, changes, flatmap=True)

    @classmethod
    def concatenate(cls, changelogs: list[ChangelogData]) -> ChangelogData:
        return cls(
            changelogs[0].paths,
            changelogs[0].config,
            ChangesData.concatenate([changelog.changes for changelog in changelogs]),
            flatmap=changelogs[0].generator_flatmap,
        )

    def add_ansible_release(
        self,
        version: str,
        date: datetime.date,
        release_summary: str,
        overwrite_release_summary: bool = True,
    ) -> None:
        add_release(
            self.config,
            self.changes,
            [],
            [],
            version,
            codename=None,
            date=date,
            update_existing=True,
            show_release_summary_warning=False,
        )
        release_date = self.changes.releases[version]
        if "changes" not in release_date:
            release_date["changes"] = {}
        if (
            "release_summary" not in release_date["changes"]
            or overwrite_release_summary
        ):
            release_date["changes"]["release_summary"] = release_summary


def read_file(tarball_path: str, matcher: t.Callable[[str], bool]) -> bytes | None:
    with tarfile.open(tarball_path, "r:gz") as tar:
        for file in tar:
            if matcher(file.name):
                file_p = tar.extractfile(file)
                if file_p:
                    with file_p:
                        return file_p.read()
    return None


def read_changelog_file(tarball_path: str, is_ansible_core=False) -> bytes | None:
    def matcher(filename: str) -> bool:
        if is_ansible_core:
            return filename.endswith("changelogs/changelog.yaml")
        return filename in ("changelogs/changelog.yaml", "changelog.yaml")

    return read_file(tarball_path, matcher)


def get_porting_guide_filename(version: PypiVer):
    return f"docs/docsite/rst/porting_guides/porting_guide_core_{version.major}.{version.minor}.rst"


class CollectionChangelogCollector:
    collection: str
    versions: list[SemVer]
    earliest: SemVer
    latest: SemVer

    changelog: ChangelogData | None

    def __init__(self, collection: str, versions: t.ValuesView[str]):
        self.collection = collection
        self.versions = sorted(SemVer(version) for version in versions)
        self.earliest = self.versions[0]
        self.latest = self.versions[-1]
        self.changelog = None

    async def _get_changelog(
        self, version: SemVer, collection_downloader: CollectionDownloader
    ) -> ChangelogData | None:
        flog = mlog.fields(func="_get_changelog")
        path = await collection_downloader.download(self.collection, version)
        changelog_bytes = read_changelog_file(path)
        if changelog_bytes is None:
            return None
        try:
            changelog_data = load_yaml_bytes(changelog_bytes)
            return ChangelogData.collection(
                self.collection, str(version), changelog_data
            )
        except Exception as exc:  # pylint: disable=broad-except
            flog.warning(
                f"Cannot load changelog of {self.collection} {version} due to {exc}"
            )
            return None

    async def _download_changelog_stream(
        self, start_version: SemVer, collection_downloader: CollectionDownloader
    ) -> ChangelogData | None:
        changelog = await self._get_changelog(start_version, collection_downloader)
        if changelog is None:
            return None

        changelog.changes.prune_versions(
            versions_after=None, versions_until=str(start_version)
        )
        changelogs = [changelog]
        ancestor = changelog.changes.ancestor
        while ancestor is not None:
            ancestor_ver = SemVer(ancestor)
            if ancestor_ver < self.earliest:
                break
            changelog = await self._get_changelog(ancestor_ver, collection_downloader)
            if changelog is None:
                break
            changelog.changes.prune_versions(
                versions_after=None, versions_until=ancestor
            )
            changelogs.append(changelog)
            ancestor = changelog.changes.ancestor

        return ChangelogData.concatenate(changelogs)

    async def download(self, collection_downloader: CollectionDownloader):
        missing_versions = set(self.versions)

        while missing_versions:
            missing_version = max(missing_versions)

            # Try to get hold of changelog for this version
            changelog = await self._download_changelog_stream(
                missing_version, collection_downloader
            )
            if changelog:
                current_changelog = self.changelog
                if current_changelog is None:
                    # If we didn't have a changelog so far, start with it
                    self.changelog = changelog
                    missing_versions -= {
                        SemVer(version) for version in changelog.changes.releases
                    }
                else:
                    # Insert entries from changelog into combined changelog that are missing there
                    for version, entry in changelog.changes.releases.items():
                        sem_version = SemVer(version)
                        if sem_version in missing_versions:
                            current_changelog.changes.releases[version] = entry
                            missing_versions.remove(sem_version)

            # Make sure that this version isn't checked again
            missing_versions -= {missing_version}


class AnsibleCoreChangelogCollector:
    versions: list[PypiVer]
    earliest: PypiVer
    latest: PypiVer

    changelog: ChangelogData | None

    porting_guide: bytes | None

    def __init__(self, versions: t.ValuesView[str]):
        self.versions = sorted(PypiVer(version) for version in versions)
        self.earliest = self.versions[0]
        self.latest = self.versions[-1]
        self.changelog = None
        self.porting_guide = None

    @staticmethod
    async def _get_changelog_file(
        version: PypiVer, core_downloader: t.Callable[[str], t.Awaitable[str]]
    ) -> ChangelogData | None:
        path = await core_downloader(str(version))
        if os.path.isdir(path):
            changelog: ChangelogData | None = None
            for root, dummy, files in os.walk(path):
                if "changelog.yaml" in files:
                    with open(os.path.join(root, "changelog.yaml"), "rb") as f:
                        changelog_bytes = f.read()
                    changelog_data = load_yaml_bytes(changelog_bytes)
                    changelog = ChangelogData.ansible_core(changelog_data)
            return changelog
        if os.path.isfile(path) and path.endswith(".tar.gz"):
            maybe_changelog_bytes = read_changelog_file(path, is_ansible_core=True)
            if maybe_changelog_bytes is None:
                return None
            changelog_data = load_yaml_bytes(maybe_changelog_bytes)
            return ChangelogData.ansible_core(changelog_data)
        return None

    async def download_changelog(
        self, core_downloader: t.Callable[[str], t.Awaitable[str]]
    ):
        changelog = await self._get_changelog_file(self.latest, core_downloader)
        if changelog is None:
            return

        changelog.changes.prune_versions(
            versions_after=None, versions_until=str(self.latest)
        )

        changelogs = [changelog]
        ancestor = changelog.changes.ancestor
        while ancestor is not None:
            ancestor_ver = PypiVer(ancestor)
            if ancestor_ver < self.earliest:
                break
            changelog = await self._get_changelog_file(ancestor_ver, core_downloader)
            if changelog is None:
                break
            changelog.changes.prune_versions(
                versions_after=None, versions_until=ancestor
            )
            changelogs.append(changelog)
            ancestor = changelog.changes.ancestor

        self.changelog = ChangelogData.concatenate(changelogs)

    async def download_porting_guide(self, aio_session: aiohttp.client.ClientSession):
        branch_url = (
            "https://raw.githubusercontent.com/ansible/ansible-documentation/devel"
        )

        query_url = f"{branch_url}/{get_porting_guide_filename(self.latest)}"
        async with aio_session.get(query_url) as response:
            self.porting_guide = await response.read()


async def collect_changelogs(
    collectors: list[CollectionChangelogCollector],
    core_collector: AnsibleCoreChangelogCollector,
    collection_cache: str | None,
    galaxy_context: GalaxyContext | None = None,
):
    lib_ctx = app_context.lib_ctx.get()
    with tempfile.TemporaryDirectory() as tmp_dir:
        async with aiohttp.ClientSession() as aio_session:
            if galaxy_context is None:
                galaxy_context = await GalaxyContext.create(aio_session)
            async with asyncio_pool.AioPool(size=lib_ctx.thread_max) as pool:
                downloader = CollectionDownloader(
                    aio_session,
                    tmp_dir,
                    context=galaxy_context,
                    collection_cache=collection_cache,
                )

                async def core_downloader(version):
                    return await get_ansible_core(aio_session, version, tmp_dir)

                requestors = [
                    await pool.spawn(collector.download(downloader))
                    for collector in collectors
                ]
                requestors.append(
                    await pool.spawn(core_collector.download_changelog(core_downloader))
                )
                requestors.append(
                    await pool.spawn(core_collector.download_porting_guide(aio_session))
                )
                await asyncio.gather(*requestors)


class ChangelogEntry:
    version: PypiVer
    version_str: str
    is_ancestor: bool

    prev_version: PypiVer | None
    core_versions: dict[PypiVer, str]
    versions_per_collection: dict[str, dict[PypiVer, str]]

    core_collector: AnsibleCoreChangelogCollector
    ansible_changelog: ChangelogData
    collectors: list[CollectionChangelogCollector]

    ansible_core_version: str
    prev_ansible_core_version: str | None

    removed_collections: list[tuple[CollectionChangelogCollector, str]]
    added_collections: list[tuple[CollectionChangelogCollector, str]]
    unchanged_collections: list[tuple[CollectionChangelogCollector, str]]
    changed_collections: list[
        tuple[CollectionChangelogCollector, str, str | None, bool]
    ]

    def __init__(
        self,
        version: PypiVer,
        version_str: str,
        prev_version: PypiVer | None,
        ancestor_version: PypiVer | None,
        core_versions: dict[PypiVer, str],
        versions_per_collection: dict[str, dict[PypiVer, str]],
        core_collector: AnsibleCoreChangelogCollector,
        ansible_changelog: ChangelogData,
        collectors: list[CollectionChangelogCollector],
    ):
        self.version = version
        self.version_str = version_str
        self.is_ancestor = (
            False if ancestor_version is None else ancestor_version == version
        )
        self.prev_version = prev_version = prev_version or ancestor_version
        self.core_versions = core_versions
        self.versions_per_collection = versions_per_collection
        self.core_collector = core_collector
        self.ansible_changelog = ansible_changelog
        self.collectors = collectors

        self.ansible_core_version = core_versions[version]
        self.prev_ansible_core_version = (
            core_versions.get(prev_version) if prev_version else None
        )

        self.removed_collections = []
        self.added_collections = []
        self.unchanged_collections = []
        self.changed_collections = []
        for collector in collectors:
            if version not in versions_per_collection[collector.collection]:
                if (
                    prev_version
                    and prev_version in versions_per_collection[collector.collection]
                ):
                    self.removed_collections.append(
                        (
                            collector,
                            versions_per_collection[collector.collection][prev_version],
                        )
                    )

                continue

            collection_version: str = versions_per_collection[collector.collection][
                version
            ]

            prev_collection_version: str | None = (
                versions_per_collection[collector.collection].get(prev_version)
                if prev_version
                else None
            )
            added = False
            if prev_version:
                if not prev_collection_version:
                    self.added_collections.append((collector, collection_version))
                    added = True
                elif prev_collection_version == collection_version:
                    self.unchanged_collections.append((collector, collection_version))
                    continue

            self.changed_collections.append(
                (collector, collection_version, prev_collection_version, added)
            )


class Changelog:
    ansible_version: PypiVer
    ansible_ancestor_version: PypiVer | None
    entries: list[ChangelogEntry]
    core_collector: AnsibleCoreChangelogCollector
    ansible_changelog: ChangelogData
    collection_collectors: list[CollectionChangelogCollector]
    collection_metadata: CollectionsMetadata

    def __init__(
        self,
        ansible_version: PypiVer,
        ansible_ancestor_version: PypiVer | None,
        entries: list[ChangelogEntry],
        core_collector: AnsibleCoreChangelogCollector,
        ansible_changelog: ChangelogData,
        collection_collectors: list[CollectionChangelogCollector],
        collection_metadata: CollectionsMetadata,
    ):
        self.ansible_version = ansible_version
        self.ansible_ancestor_version = ansible_ancestor_version
        self.entries = entries
        self.core_collector = core_collector
        self.ansible_changelog = ansible_changelog
        self.collection_collectors = collection_collectors
        self.collection_metadata = collection_metadata


def _markup_to_rst(markup: str) -> str:
    return _ansible_markup_to_rst(
        _parse_ansible_markup(
            markup,
            _AnsibleMarkupContext(),
            whitespace=_AnsibleMarkupWhitespace.KEEP_SINGLE_NEWLINES,
        )
    )


def _get_removal_entry(  # noqa: C901, pylint:disable=too-many-branches
    collection: str,
    removal: RemovalInformation,
    ansible_version: PypiVer,
) -> tuple[ChangelogFragment, str] | None:
    if (
        removal.announce_version is None
        or removal.announce_version.major != ansible_version.major
    ):
        return None

    sentences = []
    link = ""
    if removal.discussion:
        link = f" (`{removal.discussion} <{removal.discussion}>`__)"

    if removal.reason == "deprecated":
        sentences.append(f"The ``{collection}`` collection has been deprecated.")
        sentences.append(
            f"It will be removed from Ansible {removal.major_version} if no one"
            f" starts maintaining it again before Ansible {removal.major_version}."
        )
        sentences.append(
            "See `Collections Removal Process for unmaintained collections"
            " <https://docs.ansible.com/ansible/devel/community/collection_contributors/"
            "collection_package_removal.html#unmaintained-collections"
            f">`__ for more details{link}."
        )

    elif removal.reason == "considered-unmaintained":
        sentences.append(
            f"The ``{collection}`` collection is considered unmaintained"
            f" and will be removed from Ansible {removal.major_version}"
            f" if no one starts maintaining it again before Ansible {removal.major_version}."
        )
        sentences.append(
            "See `Collections Removal Process for unmaintained collections"
            " <https://docs.ansible.com/ansible/devel/community/collection_contributors/"
            "collection_package_removal.html#unmaintained-collections"
            f">`__ for more details, including for how this can be cancelled{link}."
        )

    elif removal.reason == "renamed":
        sentences.append(
            f"The collection ``{collection}`` was renamed to ``{removal.new_name}``."
        )
        sentences.append("For now both collections are included in Ansible.")
        if removal.redirect_replacement_major_version is not None:
            if ansible_version.major < removal.redirect_replacement_major_version:
                sentences.append(
                    f"The content in ``{collection}`` will be replaced by deprecated"
                    f" redirects in Ansible {removal.redirect_replacement_major_version}.0.0."
                )
            else:
                sentences.append(
                    f"The content in ``{collection}`` has been replaced by deprecated"
                    f" redirects in Ansible {removal.redirect_replacement_major_version}.0.0."
                )
        if removal.major_version != "TBD":
            sentences.append(
                f"The collection will be completely removed from Ansible {removal.major_version}."
            )
        else:
            sentences.append(
                "The collection will be completely removed from Ansible eventually."
            )
        sentences.append(
            f"Please update your FQCNs from ``{collection}`` to ``{removal.new_name}``{link}."
        )

    elif removal.reason == "guidelines-violation":
        sentences.append(
            f"The {collection} collection will be removed from Ansible {removal.major_version}"
            " due to violations of the Ansible inclusion requirements."
        )
        if removal.reason_text:
            sentences.append(_markup_to_rst(removal.reason_text))
        sentences.append(
            "See `Collections Removal Process for collections"
            " not satisfying the collection requirements"
            " <https://docs.ansible.com/ansible/devel/community/collection_contributors/"
            "collection_package_removal.html#collections-not-satisfying-the-collection-requirements"
            f">`__ for more details, including for how this can be cancelled{link}."
        )

    elif removal.reason == "other":
        sentences.append(
            f"The {collection} collection will be removed from Ansible {removal.major_version}."
        )
        if removal.reason_text:
            sentences.append(_markup_to_rst(removal.reason_text))
        if removal.discussion:
            sentences.append(
                f"See `the removal discussion for details <{removal.discussion}>`__."
            )
        else:
            sentences.append(
                "To discuss this, please `create a community topic"
                " <https://docs.ansible.com/ansible/devel/community/steering/"
                "community_steering_committee.html#creating-community-topic>`__."
            )

    if not sentences:
        return None
    return ChangelogFragment(
        content={
            "deprecated_features": [
                "\n".join(sentences),
            ],
        },
        path="<internal>",
        fragment_format=TextFormat.RESTRUCTURED_TEXT,
    ), str(removal.announce_version)


def _get_removed_entry(  # noqa: C901, pylint:disable=too-many-branches
    collection: str,
    removal: RemovedRemovalInformation,
    ansible_version: PypiVer,
) -> tuple[ChangelogFragment, str] | None:
    if ansible_version.major != removal.version.major:
        return None

    sentences = []
    link = ""
    if removal.discussion:
        link = f" (`{removal.discussion} <{removal.discussion}>`__)"

    if removal.reason == "deprecated":
        sentences.append(
            f"The deprecated ``{collection}`` collection has been removed{link}."
        )

    elif removal.reason == "considered-unmaintained":
        sentences.append(
            f"The ``{collection}`` collection was considered unmaintained"
            f" and has been removed from Ansible {removal.version.major}{link}."
        )

    elif removal.reason == "renamed":
        sentences.append(
            f"The collection ``{collection}`` has been completely removed from Ansible."
        )
        sentences.append(f"It has been renamed to ``{removal.new_name}``.")
        if removal.redirect_replacement_major_version is not None:
            sentences.append(
                f"``{collection}`` has been replaced by deprecated redirects to"
                f" ``{removal.new_name}``"
                f" in Ansible {removal.redirect_replacement_major_version}.0.0."
            )
        else:
            sentences.append(
                "The collection will be completely removed from Ansible eventually."
            )
        sentences.append(
            f"Please update your FQCNs from ``{collection}`` to ``{removal.new_name}``{link}."
        )

    elif removal.reason == "guidelines-violation":
        sentences.append(
            f"The {collection} collection has been removed from Ansible {removal.version.major}"
            " due to violations of the Ansible inclusion requirements."
        )
        if removal.reason_text:
            sentences.append(_markup_to_rst(removal.reason_text))
        sentences.append(
            "See `Collections Removal Process for collections"
            " not satisfying the collection requirements"
            " <https://docs.ansible.com/ansible/devel/community/collection_contributors/"
            "collection_package_removal.html#collections-not-satisfying-the-collection-requirements"
            f">`__ for more details{link}."
        )

    elif removal.reason == "other":
        sentences.append(
            f"The {collection} collection has been removed from Ansible {removal.version.major}."
        )
        if removal.reason_text:
            sentences.append(_markup_to_rst(removal.reason_text))
        if removal.discussion:
            sentences.append(
                f"See `the removal discussion <{removal.discussion}>`__ for details."
            )
        else:
            sentences.append(
                "To discuss this, please `create a community topic"
                " <https://docs.ansible.com/ansible/devel/community/steering/"
                "community_steering_committee.html#creating-community-topic>`__."
            )

    if sentences and removal.reason not in ("renamed", "deprecated"):
        sentences.append(
            "Users can still install this collection with "
            f"``ansible-galaxy collection install {collection}``."
        )

    if not sentences:
        return None
    return ChangelogFragment(
        content={
            "removed_features": [
                "\n".join(sentences),
            ],
        },
        path="<internal>",
        fragment_format=TextFormat.RESTRUCTURED_TEXT,
    ), str(removal.version)


def _populate_ansible_changelog(
    ansible_changelog: ChangelogData,
    collection_metadata: CollectionsMetadata,
    ansible_version: PypiVer,
) -> None:
    flog = mlog.fields(func="_populate_ansible_changelog")
    for collection, metadata in collection_metadata.collections.items():
        if metadata.removal:
            fragment_version = _get_removal_entry(
                collection, metadata.removal, ansible_version
            )
            if fragment_version:
                fragment, version = fragment_version
                if version in ansible_changelog.changes.releases:
                    ansible_changelog.changes.add_fragment(fragment, version)
                else:
                    flog.warning(
                        f"Found changelog entry for {version}, which does not yet exist"
                    )

    for collection, removed_metadata in collection_metadata.removed_collections.items():
        fragment_version = _get_removed_entry(
            collection, removed_metadata.removal, ansible_version
        )
        if fragment_version:
            fragment, version = fragment_version
            if version in ansible_changelog.changes.releases:
                ansible_changelog.changes.add_fragment(fragment, version)
            else:
                flog.warning(
                    f"Found changelog entry for {version}, which does not yet exist"
                )


def get_changelog(
    ansible_version: PypiVer,
    deps_dir: str | None,
    deps_data: list[DependencyFileData] | None = None,
    collection_cache: str | None = None,
    ansible_changelog: ChangelogData | None = None,
    galaxy_context: GalaxyContext | None = None,
) -> Changelog:
    flog = mlog.fields(func="get_changelog")
    dependencies: dict[str, DependencyFileData] = {}

    ansible_changelog = ansible_changelog or ChangelogData.ansible(directory=deps_dir)
    ansible_ancestor_version_str = ansible_changelog.changes.ancestor
    ansible_ancestor_version = (
        PypiVer(ansible_ancestor_version_str) if ansible_ancestor_version_str else None
    )

    collection_metadata = CollectionsMetadata.load_from(deps_dir)
    _populate_ansible_changelog(ansible_changelog, collection_metadata, ansible_version)

    if deps_dir is not None:
        for path in glob.glob(os.path.join(deps_dir, "*.deps"), recursive=False):
            deps_file = DepsFile(path)
            deps = deps_file.parse()
            deps.deps.pop("_python", None)
            version = PypiVer(deps.ansible_version)
            if version > ansible_version:
                flog.info(
                    f"Ignoring {path}, since {deps.ansible_version}"
                    f" is newer than {ansible_version}"
                )
                continue
            dependencies[deps.ansible_version] = deps
    if deps_data:
        for deps in deps_data:
            dependencies[deps.ansible_version] = deps

    core_versions: dict[PypiVer, str] = {}
    versions: dict[str, tuple[PypiVer, DependencyFileData]] = {}
    versions_per_collection: dict[str, dict[PypiVer, str]] = defaultdict(dict)
    for deps in dependencies.values():
        version = PypiVer(deps.ansible_version)
        versions[deps.ansible_version] = (version, deps)
        core_versions[version] = deps.ansible_core_version
        for collection_name, collection_version in deps.deps.items():
            versions_per_collection[collection_name][version] = collection_version

    core_collector = AnsibleCoreChangelogCollector(core_versions.values())
    collectors = [
        CollectionChangelogCollector(
            collection, versions_per_collection[collection].values()
        )
        for collection in sorted(versions_per_collection.keys())
    ]
    asyncio.run(
        collect_changelogs(collectors, core_collector, collection_cache, galaxy_context)
    )

    changelog = []

    sorted_versions = collect_versions(versions, ansible_changelog.config)
    for index, (version_str, dummy) in enumerate(sorted_versions):
        version, deps = versions[version_str]
        prev_version = None
        if index + 1 < len(sorted_versions):
            prev_version = versions[sorted_versions[index + 1][0]][0]

        changelog.append(
            ChangelogEntry(
                version,
                version_str,
                prev_version,
                ansible_ancestor_version,
                core_versions,
                versions_per_collection,
                core_collector,
                ansible_changelog,
                collectors,
            )
        )

    return Changelog(
        ansible_version,
        ansible_ancestor_version,
        changelog,
        core_collector,
        ansible_changelog,
        collectors,
        collection_metadata,
    )
