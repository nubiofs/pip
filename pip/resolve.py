"""Dependency Resolution

The dependency resolution in pip is performed as follows:

for top-level requirements:
    a. only one spec allowed per project, regardless of conflicts or not.
       otherwise a "double requirement" exception is raised
    b. they override sub-dependency requirements.
for sub-dependencies
    a. "first found, wins" (where the order is breadth first)
"""

import logging
from itertools import chain

from pip.exceptions import (
    BestVersionAlreadyInstalled,
    DistributionNotFound, HashError, HashErrors, UnsupportedPythonVersion
)
from pip.req.req_install import InstallRequirement
from pip.utils import dist_in_usersite, ensure_dir
from pip.utils.logging import indent_log
from pip.utils.packaging import check_dist_requires_python

logger = logging.getLogger(__name__)


class Resolver(object):
    """Resolves which packages need to be installed/uninstalled to perform \
    the requested operation without breaking the requirements of any package.
    """

    _allowed_strategies = {"eager", "only-if-needed", "to-satisfy-only"}

    def __init__(self, preparer, session, finder, use_user_site,
                 ignore_dependencies, ignore_installed, ignore_requires_python,
                 force_reinstall, isolated, upgrade_strategy):
        super(Resolver, self).__init__()
        assert upgrade_strategy in self._allowed_strategies

        self.preparer = preparer
        self.finder = finder
        self.session = session

        self.require_hashes = None  # This is set in resolve

        self.upgrade_strategy = upgrade_strategy
        self.force_reinstall = force_reinstall
        self.isolated = isolated
        self.ignore_dependencies = ignore_dependencies
        self.ignore_installed = ignore_installed
        self.ignore_requires_python = ignore_requires_python
        self.use_user_site = use_user_site

    def resolve(self, requirement_set):
        """Resolve what operations need to be done

        As a side-effect of this method, the packages (and their dependencies)
        are downloaded, unpacked and prepared for installation.

        Once PyPI has static dependency metadata available, it would be
        possible to move this side-effect to become a step separated from
        dependency resolution.
        """
        # make the wheelhouse
        if requirement_set.wheel_download_dir:
            ensure_dir(requirement_set.wheel_download_dir)

        # If any top-level requirement has a hash specified, enter
        # hash-checking mode, which requires hashes from all.
        root_reqs = (
            requirement_set.unnamed_requirements +
            requirement_set.requirements.values()
        )
        self.require_hashes = (
            requirement_set.require_hashes or
            any(req.has_hash_options for req in root_reqs)
        )

        # Display where finder is looking for packages
        locations = self.finder.get_formatted_locations()
        if locations:
            logger.info(locations)

        # Actually prepare the files, and collect any exceptions. Most hash
        # exceptions cannot be checked ahead of time, because
        # req.populate_link() needs to be called before we can make decisions
        # based on link type.
        discovered_reqs = []
        hash_errors = HashErrors()
        for req in chain(root_reqs, discovered_reqs):
            try:
                discovered_reqs.extend(
                    self._resolve_one(requirement_set, req)
                )
            except HashError as exc:
                exc.req = req
                hash_errors.append(exc)

        if hash_errors:
            raise hash_errors

    def _is_upgrade_allowed(self, req):
        if self.upgrade_strategy == "to-satisfy-only":
            return False
        elif self.upgrade_strategy == "eager":
            return True
        else:
            assert self.upgrade_strategy == "only-if-needed"
            return req.is_direct

    # XXX: Stop passing requirement_set for options
    def _check_skip_installed(self, req_to_install):
        """Check if req_to_install should be skipped.

        This will check if the req is installed, and whether we should upgrade
        or reinstall it, taking into account all the relevant user options.

        After calling this req_to_install will only have satisfied_by set to
        None if the req_to_install is to be upgraded/reinstalled etc. Any
        other value will be a dist recording the current thing installed that
        satisfies the requirement.

        Note that for vcs urls and the like we can't assess skipping in this
        routine - we simply identify that we need to pull the thing down,
        then later on it is pulled down and introspected to assess upgrade/
        reinstalls etc.

        :return: A text reason for why it was skipped, or None.
        """
        # Check whether to upgrade/reinstall this req or not.
        req_to_install.check_if_exists()
        if req_to_install.satisfied_by:
            upgrade_allowed = self._is_upgrade_allowed(req_to_install)

            # Is the best version is installed.
            best_installed = False

            if upgrade_allowed:
                # For link based requirements we have to pull the
                # tree down and inspect to assess the version #, so
                # its handled way down.
                should_check_possibility_for_upgrade = not (
                    self.force_reinstall or req_to_install.link
                )
                if should_check_possibility_for_upgrade:
                    try:
                        self.finder.find_requirement(
                            req_to_install, upgrade_allowed)
                    except BestVersionAlreadyInstalled:
                        best_installed = True
                    except DistributionNotFound:
                        # No distribution found, so we squash the
                        # error - it will be raised later when we
                        # re-try later to do the install.
                        # Why don't we just raise here?
                        pass

                if not best_installed:
                    # don't uninstall conflict if user install and
                    # conflict is not user install
                    if not (self.use_user_site and not
                            dist_in_usersite(req_to_install.satisfied_by)):
                        req_to_install.conflicts_with = \
                            req_to_install.satisfied_by
                    req_to_install.satisfied_by = None

            # Figure out a nice message to say why we're skipping this.
            if best_installed:
                skip_reason = 'already up-to-date'
            elif self.upgrade_strategy == "only-if-needed":
                skip_reason = 'not upgraded as not directly required'
            else:
                skip_reason = 'already satisfied'

            return skip_reason
        else:
            return None

    def _resolve_one(self, requirement_set, req_to_install):
        """Prepare a single requirements file.

        :return: A list of additional InstallRequirements to also install.
        """
        # Tell user what we are doing for this requirement:
        # obtain (editable), skipping, processing (local url), collecting
        # (remote url or package name)
        if req_to_install.constraint or req_to_install.prepared:
            return []

        req_to_install.prepared = True
        abstract_dist = self.preparer.prepare_requirement(
            req_to_install, self, requirement_set
        )

        # register tmp src for cleanup in case something goes wrong
        requirement_set.reqs_to_cleanup.append(req_to_install)

        # Parse and return dependencies
        dist = abstract_dist.dist(self.finder)
        try:
            check_dist_requires_python(dist)
        except UnsupportedPythonVersion as err:
            if self.ignore_requires_python:
                logger.warning(err.args[0])
            else:
                raise

        more_reqs = []

        def add_req(subreq, extras_requested):
            sub_install_req = InstallRequirement.from_req(
                str(subreq),
                req_to_install,
                isolated=self.isolated,
                wheel_cache=requirement_set._wheel_cache,
            )
            more_reqs.extend(
                requirement_set.add_requirement(
                    sub_install_req, req_to_install.name,
                    extras_requested=extras_requested
                )
            )

        with indent_log():
            # We add req_to_install before its dependencies, so that we
            # can refer to it when adding dependencies.
            if not requirement_set.has_requirement(req_to_install.name):
                # 'unnamed' requirements will get added here
                requirement_set.add_requirement(req_to_install, None)

            if not self.ignore_dependencies:
                if req_to_install.extras:
                    logger.debug(
                        "Installing extra requirements: %r",
                        ','.join(req_to_install.extras),
                    )
                missing_requested = sorted(
                    set(req_to_install.extras) - set(dist.extras)
                )
                for missing in missing_requested:
                    logger.warning(
                        '%s does not provide the extra \'%s\'',
                        dist, missing
                    )

                available_requested = sorted(
                    set(dist.extras) & set(req_to_install.extras)
                )
                for subreq in dist.requires(available_requested):
                    add_req(subreq, extras_requested=available_requested)

            if not req_to_install.editable and not req_to_install.satisfied_by:
                # XXX: --no-install leads this to report 'Successfully
                # downloaded' for only non-editable reqs, even though we took
                # action on them.
                requirement_set.successfully_downloaded.append(req_to_install)

        return more_reqs
