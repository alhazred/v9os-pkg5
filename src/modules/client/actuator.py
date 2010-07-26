#!/usr/bin/python
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2008, 2010, Oracle and/or its affiliates. All rights reserved.
#

import os
import pkg.pkgsubprocess as subprocess
from pkg.client import global_settings
from pkg.client.debugvalues import DebugValues


# range of possible SMF service states
SMF_SVC_UNKNOWN      = 0
SMF_SVC_DISABLED     = 1
SMF_SVC_MAINTENANCE  = 2
SMF_SVC_TMP_DISABLED = 3
SMF_SVC_TMP_ENABLED  = 4
SMF_SVC_ENABLED      = 5

logger = global_settings.logger

svcprop_path = "/usr/bin/svcprop"
svcadm_path  = "/usr/sbin/svcadm"
svcs_path = "/usr/bin/svcs"


class NonzeroExitException(Exception):
        def __init__(self, cmd, return_code, output):
                self.cmd = cmd
                self.return_code = return_code
                self.output = output

        def __unicode__(self):
                # To workaround python issues 6108 and 2517, this provides a
                # a standard wrapper for this class' exceptions so that they
                # have a chance of being stringified correctly.
                return str(self)

        def __str__(self):
                return "Cmd %s exited with status %d, and output '%s'" %\
                    (self.cmd, self.return_code, self.output)


class GenericActuator(object):
        """Actuators are action attributes that cause side effects
        on live images when those actions are updated, installed
        or removed.  Since no side effects are caused when the
        affected image isn't the current root image, the OS may
        need to cause the equivalent effect during boot.
        """

        actuator_attrs = set()

        def __init__(self):
                self.install = {}
                self.removal = {}
                self.update =  {}

        def __nonzero__(self):
                return bool(self.install or self.removal or self.update)

        def scan_install(self, attrs):
                self.__scan(self.install, attrs)

        def scan_removal(self, attrs):
                self.__scan(self.removal, attrs)

        def scan_update(self, attrs):
                self.__scan(self.update, attrs)

        def __scan(self, dictionary, attrs):
                for a in set(attrs.keys()) & self.actuator_attrs:
                        values = attrs[a]

                        if not isinstance(values, list):
                                values = [values]

                        dictionary.setdefault(a, set()).update(values)

        def reboot_needed(self):
                return False

        def exec_prep(self, image):
                pass

        def exec_pre_actuators(self, image):
                pass

        def exec_post_actuators(self, image):
                pass

        def exec_fail_actuators(self, image):
                pass

        def __str__(self):
                return "Removals: %s\nInstalls: %s\nUpdates: %s\n" % \
                    (self.removal, self.install, self.update)


class Actuator(GenericActuator):
        """Solaris specific Actuator implementation..."""

        actuator_attrs = set([
            "reboot-needed",    # have to reboot to update this file
            "refresh_fmri",     # refresh this service on any change
            "restart_fmri",     # restart this service on any change
            "suspend_fmri",     # suspend this service during update
            "disable_fmri"      # disable this service prior to removal
        ])

        def __init__(self):
                GenericActuator.__init__(self)
                self.suspend_fmris = None
                self.tmp_suspend_fmris = None
                self.do_nothing = True
                self.cmd_path = ""

        def __str__(self):
                def check_val(dfmri):
                        # For actuators which are a single, global function that
                        # needs to get executed, simply print true.
                        if callable(dfmri):
                                return [ "true" ]
                        else:
                                return dfmri

                merge = {}
                for d in [self.removal, self.update, self.install]:
                        for a in d.keys():
                                for smf in check_val(d[a]):
                                        merge.setdefault(a, set()).add(smf)

                if self.reboot_needed():
                        merge["reboot-needed"] = set(["true"])
                else:
                        merge["reboot-needed"] = set(["false"])

                return "\n".join([
                    "  %16s: %s" % (fmri, smf)
                    for fmri in merge
                    for smf in merge[fmri]
                ])

        def reboot_needed(self):
                return bool("true" in self.update.get("reboot-needed", [])) or \
                    bool("true" in self.removal.get("reboot-needed", []))

        def exec_prep(self, image):
                if not image.is_liveroot():
                        cmds_dir = DebugValues.get_value("actuator_cmds_dir")
                        if not cmds_dir:
                                return
                        self.cmd_path = cmds_dir
                self.do_nothing = False

        def exec_pre_actuators(self, image):
                """do pre execution actuator processing..."""

                if self.do_nothing:
                        return

                suspend_fmris = self.update.get("suspend_fmri", set())
                tmp_suspend_fmris = set()

                disable_fmris = self.removal.get("disable_fmri", set())

                suspend_fmris = self.__smf_svc_check_fmris("suspend_fmri", suspend_fmris)
                disable_fmris = self.__smf_svc_check_fmris("disable_fmri", disable_fmris)
                # eliminate services not loaded or not running
                # remember those services enabled only temporarily

                for fmri in suspend_fmris.copy():
                        state = self.__smf_svc_get_state(fmri)
                        if state <= SMF_SVC_TMP_ENABLED:
                                suspend_fmris.remove(fmri)
                        if state == SMF_SVC_TMP_ENABLED:
                                tmp_suspend_fmris.add(fmri)

                for fmri in disable_fmris.copy():
                        if self.__smf_svc_is_disabled(fmri):
                                disable_fmris.remove(fmri)

                self.suspend_fmris = suspend_fmris
                self.tmp_suspend_fmris = tmp_suspend_fmris

                args = (svcadm_path, "disable", "-st")

                params = tuple(suspend_fmris | tmp_suspend_fmris)

                if params:
                        self.__call(args + params)

                args = (svcadm_path, "disable",  "-s")
                params = tuple(disable_fmris)

                if params:
                        self.__call(args + params)

        def exec_fail_actuators(self, image):
                """handle a failed install"""

                if self.do_nothing:
                        return

                args = (svcadm_path, "mark", "maintenance")
                params = tuple(self.suspend_fmris |
                    self.tmp_suspend_fmris)

                if params:
                        self.__call(args + params)

        def exec_post_actuators(self, image):
                """do post execution actuator processing"""

                if self.do_nothing:
                        return

                refresh_fmris = self.removal.get("refresh_fmri", set()) | \
                    self.update.get("refresh_fmri", set()) | \
                    self.install.get("refresh_fmri", set())

                restart_fmris = self.removal.get("restart_fmri", set()) | \
                    self.update.get("restart_fmri", set()) | \
                    self.install.get("restart_fmri", set())

                refresh_fmris = self.__smf_svc_check_fmris("refresh_fmri", refresh_fmris)
                restart_fmris = self.__smf_svc_check_fmris("restart_fmri", restart_fmris)

                # ignore services not present or not
                # enabled

                for fmri in refresh_fmris.copy():
                        if self.__smf_svc_is_disabled(fmri):
                                refresh_fmris.remove(fmri)

                args = (svcadm_path, "refresh")
                params = tuple(refresh_fmris)

                if params:
                        self.__call(args + params)

                for fmri in restart_fmris.copy():
                        if self.__smf_svc_is_disabled(fmri):
                                restart_fmris.remove(fmri)

                args = (svcadm_path, "restart")
                params = tuple(restart_fmris)
                if params:
                        self.__call(args + params)

                # reenable suspended services that were running
                # be sure to not enable services that weren't running
                # and temp. enable those services that were in that
                # state.

                args = (svcadm_path, "enable")
                params = tuple(self.suspend_fmris)
                if params:
                        self.__call(args + params)

                args = (svcadm_path, "enable", "-t")
                params = tuple(self.tmp_suspend_fmris)
                if params:
                        self.__call(args + params)

                for act in self.install.itervalues():
                        if callable(act):
                                act()

        def __smf_svc_get_state(self, fmri):
                """ return state of smf service """

                props = self.__get_smf_props(fmri)
                if not props:
                        return SMF_SVC_UNKNOWN

                if "maintenance" in props["restarter/state"]:
                        return SMF_SVC_MAINTENANCE

                if "true" not in props["general/enabled"]:
                        if "general_ovr/enabled" not in props:
                                return SMF_SVC_DISABLED
                        elif "true" in props["general_ovr/enabled"]:
                                return SMF_SVC_TMP_ENABLED
                else:
                        if "general_ovr/enabled" not in props:
                                return SMF_SVC_ENABLED
                        elif "false" in props["general_ovr/enabled"]:
                                return SMF_SVC_TMP_DISABLED

        def __smf_svc_is_disabled(self, fmri):
                return self.__smf_svc_get_state(fmri) < SMF_SVC_TMP_ENABLED

        def __smf_svc_check_fmris(self, attr, fmris):
                """ Walk a set of fmris checking that each is fully specifed with
                an instance.
                If an FMRI is not fully specified and does not contain at least
                one special match character from fnmatch(5) the fmri is dropped
                from the set that is returned and an error message is logged.
                """

                chars = "*?[!^"
                for fmri in fmris.copy():
                        is_glob = False
                        for c in chars:
                                if c in fmri:
                                        is_glob = True

                        tmp_fmri = fmri
                        if fmri.startswith("svc:"):
                                tmp_fmri = fmri.replace("svc:", "", 1)

                        # check to see if we've got an instance already
                        if ":" in tmp_fmri and not is_glob:
                                continue

                        if is_glob:
                                cmd = (svcs_path, "-H", "-o", "fmri", "%s" % fmri)
                                try:
                                        instances = self.__call(cmd)
                                except NonzeroExitException:
                                        continue # non-zero exit == not installed

                        else:
                                instances = []
                                logger.error(_("FMRI pattern might implicitly match " \
                                    "more than one service instance."))
                                logger.error(_("Actuators for %(attr)s will not be run " \
                                    "for %(fmri)s.") % locals())

                        fmris.remove(fmri)
                        for instance in instances:
                                fmris.add(instance.rstrip())
                return fmris

        def __get_smf_props(self, svcfmri):
                args = (svcprop_path, "-c", svcfmri)

                try:
                        buf = self.__call(args)
                except NonzeroExitException:
                        return {} # empty output == not installed

                return dict([
                    l.strip().split(None, 1)
                    for l in buf
                ])

        def __call(self, args):
                # a way to invoke a separate executable for testing
                if self.cmd_path:
                        args = (
                            os.path.join(self.cmd_path,
                            args[0].lstrip("/")),) + args[1:]
                try:
                        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
                        buf = proc.stdout.readlines()
                        ret = proc.wait()
                except OSError, e:
                        raise RuntimeError, "cannot execute %s: %s" % (args, e)

                if ret != 0:
                        raise NonzeroExitException(args, ret, buf)
                return buf
