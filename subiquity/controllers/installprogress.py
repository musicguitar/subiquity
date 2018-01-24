# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import fcntl
import logging
import os
import subprocess
import sys

import yaml

from systemd import journal

from subiquitycore import utils
from subiquitycore.controller import BaseController

from subiquity.curtin import (
    CURTIN_INSTALL_LOG,
    CURTIN_POSTINSTALL_LOG,
    curtin_install_cmd,
    )
from subiquity.ui.views import ProgressView


log = logging.getLogger("subiquitycore.controller.installprogress")


class InstallState:
    NOT_STARTED = 0
    RUNNING_INSTALL = 1
    DONE_INSTALL = 2
    RUNNING_POSTINSTALL = 3
    DONE_POSTINSTALL = 4
    ERROR_INSTALL = -1
    ERROR_POSTINSTALL = -2


class InstallProgressController(BaseController):
    signals = [
        ('installprogress:filesystem-config-done', 'filesystem_config_done'),
        ('installprogress:identity-config-done',   'identity_config_done'),
    ]

    def __init__(self, common):
        super().__init__(common)
        self.answers = self.all_answers.get('InstallProgress', {})
        self.answers.setdefault('reboot', False)
        self.progress_view = None
        self.install_state = InstallState.NOT_STARTED
        self.postinstall_written = False
        self.tail_proc = None
        self.journald_forwarder_proc = None
        self.curtin_event_stack = []

    def filesystem_config_done(self):
        self.curtin_start_install()

    def identity_config_done(self):
        self.postinstall_written = True
        if self.install_state == InstallState.DONE_INSTALL:
            self.curtin_start_postinstall()

    def curtin_error(self):
        log.debug('curtin_error')
        title = _('An error occurred during installation')
        self.ui.set_header(title, _('Please report this error in Launchpad'))
        self.ui.set_footer(_("An error has occurred."))
        if self.progress_view is not None:
            self.progress_view.set_status(('info_error', "An error has occurred"))
            self.progress_view.show_complete(True)
        else:
            self.default()

    def run_command_logged(self, cmd, logfile_location, env):
        with open(logfile_location, 'wb', buffering=0) as logfile:
            log.debug("running %s", cmd)
            cp = subprocess.run(
                cmd, env=env, stdout=logfile, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            log.debug("completed %s", cmd)
        return cp.returncode

    def curtin_event(self, event):
        event_type = event.get("CURTIN_EVENT_TYPE")
        if event_type not in ['start', 'finish']:
            return
        if event_type == 'start':
            desc = event["MESSAGE"]
            self.curtin_event_stack.append(desc)
        if event_type == 'finish':
            if not self.curtin_event_stack:
                return
            self.curtin_event_stack.pop()
            if self.curtin_event_stack:
                desc = self.curtin_event_stack[-1]
            else:
                desc = ""
        self.ui.set_footer("Running install... %s" % (desc,))

    def start_journald_listener(self, identifier, callback):
        reader = journal.Reader()
        reader.seek_tail()
        reader.add_match("SYSLOG_IDENTIFIER={}".format(identifier))
        def watch():
            if reader.process() != journal.APPEND:
                return
            for event in reader:
                callback(event)
        self.loop.watch_file(reader.fileno(), watch)

    def _write_config(self, path, config):
        with open(path, 'w') as conf:
            datestr = '# Autogenerated by SUbiquity: {} UTC\n'.format(
                str(datetime.datetime.utcnow()))
            conf.write(datestr)
            conf.write(yaml.dump(config))

    def _get_curtin_command(self, install_step, storage_conf=None):
        configs = []
        config_file_name = 'subiquity-curtin-%s.conf' % (install_step,)

        if storage_conf is not None:
            if install_step == "install":
                configs.append(storage_conf)
            elif install_step == "postinstall":
                storage_post_conf = '/tmp/subiquity-config-storage-postinstall.yaml'
                storage_data = yaml.safe_load(open(storage_conf).read())
                for v in storage_data['storage']['config']:
                    v.update({'preserve': True})
                with open(storage_post_conf, 'w') as outfile:
                    yaml.dump(storage_data, outfile)
                configs.append(storage_post_conf)

        if self.opts.dry_run:
            config_location = os.path.join('.subiquity/', config_file_name)
            log.debug("Installprogress: this is a dry-run")
            curtin_cmd = [
                "python3", "scripts/replay-curtin-log.py",
                self.reporting_url, "examples/curtin-events-%s.json" % (install_step,),
                ]
        else:
            config_location = os.path.join('/tmp', config_file_name)
            log.debug("Installprogress: this is the *REAL* thing")
            configs.append(config_location)
            curtin_cmd = curtin_install_cmd(configs)

        self._write_config(
            config_location,
            self.base_model.render(
                install_step=install_step, reporting_url=self.reporting_url, storage_conf=storage_conf))

        return curtin_cmd

    def curtin_start_install(self):
        log.debug('Curtin Install: calling curtin with '
                  'storage/net/postinstall config')
        self.install_state = InstallState.RUNNING_INSTALL
        self.start_journald_forwarder()
        self.start_journald_listener("curtin_event", self.curtin_event)

        curtin_cmd = self._get_curtin_command("install", storage_conf=self.opts.storage_conf)

        log.debug('Curtin install cmd: {}'.format(curtin_cmd))
        env = os.environ.copy()
        if 'SNAP' in env:
            del env['SNAP']
        self.run_in_bg(
            lambda: self.run_command_logged(curtin_cmd, CURTIN_INSTALL_LOG, env),
            self.curtin_install_completed)

    def curtin_install_completed(self, fut):
        returncode = fut.result()
        log.debug('curtin_install: returncode: {}'.format(returncode))
        if returncode > 0:
            self.install_state = InstallState.ERROR_INSTALL
            self.curtin_error()
            return
        self.stop_tail_proc()
        self.install_state = InstallState.DONE_INSTALL
        log.debug('After curtin install OK')
        if self.postinstall_written:
            self.curtin_start_postinstall()

    def cancel(self):
        pass

    def curtin_start_postinstall(self):
        log.debug('Curtin Post Install: calling curtin '
                  'with postinstall config')

        if not self.postinstall_written:
            log.error('Attempting to spawn curtin install without a config')
            raise Exception('AIEEE!')

        self.install_state = InstallState.RUNNING_POSTINSTALL
        if self.progress_view is not None:
            self.progress_view.clear_log_tail()
            self.progress_view.set_status(_("Running postinstall step"))
            self.start_tail_proc()

        curtin_cmd = self._get_curtin_command("postinstall", storage_conf=self.opts.storage_conf)

        log.debug('Curtin postinstall cmd: {}'.format(curtin_cmd))
        env = os.environ.copy()
        if 'SNAP' in env:
            del env['SNAP']
        self.run_in_bg(
            lambda: self.run_command_logged(curtin_cmd, CURTIN_POSTINSTALL_LOG, env),
            self.curtin_postinstall_completed)

    def curtin_postinstall_completed(self, fut):
        returncode = fut.result()
        log.debug('curtin_postinstall: returncode: {}'.format(returncode))
        self.stop_tail_proc()
        if returncode > 0:
            self.install_state = InstallState.ERROR_POSTINSTALL
            self.curtin_error()
            return
        log.debug('After curtin postinstall OK')
        self.install_state = InstallState.DONE_POSTINSTALL
        self.ui.progress_current += 1
        self.ui.set_header(_("Installation complete!"), "")
        self.ui.set_footer("")
        self.progress_view.set_status(_("Finished install!"))
        self.progress_view.show_complete()
        if self.answers['reboot']:
            self.loop.set_alarm_in(0.01, lambda loop, userdata: self.reboot())

    def update_log_tail(self):
        if self.tail_proc is None:
            return
        tail = self.tail_proc.stdout.read().decode('utf-8', 'replace')
        self.progress_view.add_log_tail(tail)

    def start_journald_forwarder(self):
        log.debug("starting curtin journald forwarder")
        if "SNAP" in os.environ and sys.executable.startswith(os.environ["SNAP"]):
            script = os.path.join(os.environ["SNAP"], 'usr/bin/curtin-journald-forwarder')
        else:
            script = './bin/curtin-journald-forwarder'
        self.journald_forwarder_proc = utils.run_command_start([script])
        self.reporting_url = self.journald_forwarder_proc.stdout.readline().decode('utf-8').strip()
        log.debug("curtin journald forwarder listening on %s", self.reporting_url)

    def start_tail_proc(self):
        if self.install_state == InstallState.ERROR_INSTALL:
            install_log = CURTIN_INSTALL_LOG
        elif self.install_state == InstallState.ERROR_POSTINSTALL:
            install_log = CURTIN_POSTINSTALL_LOG
        elif self.install_state < InstallState.RUNNING_POSTINSTALL:
            install_log = CURTIN_INSTALL_LOG
        else:
            install_log = CURTIN_POSTINSTALL_LOG
        self.progress_view.clear_log_tail()
        tail_cmd = ['tail', '-n', '1000', '-F', install_log]
        log.debug('tail cmd: {}'.format(" ".join(tail_cmd)))
        self.tail_proc = utils.run_command_start(tail_cmd)
        stdout_fileno = self.tail_proc.stdout.fileno()
        fcntl.fcntl(
            stdout_fileno, fcntl.F_SETFL,
            fcntl.fcntl(stdout_fileno, fcntl.F_GETFL) | os.O_NONBLOCK)
        self.tail_watcher_handle = self.loop.watch_file(stdout_fileno, self.update_log_tail)

    def stop_tail_proc(self):
        if self.tail_proc is not None:
            self.loop.remove_watch_file(self.tail_watcher_handle)
            self.tail_proc.terminate()
            self.tail_proc = None

    def reboot(self):
        if self.opts.dry_run:
            log.debug('dry-run enabled, skipping reboot, quiting instead')
            self.signal.emit_signal('quit')
        else:
            utils.run_command(["/sbin/reboot"])

    def quit(self):
        if not self.opts.dry_run:
            utils.disable_subiquity()
        self.signal.emit_signal('quit')

    def default(self):
        log.debug('show_progress called')
        title = _("Installing system")
        excerpt = _("Please wait for the installation to finish.")
        footer = _("Thank you for using Ubuntu!")
        self.ui.set_header(title, excerpt)
        self.ui.set_footer(footer)
        self.progress_view = ProgressView(self)
        self.start_tail_proc()
        if self.install_state < 0:
            self.curtin_error()
            self.ui.set_body(self.progress_view)
            return
        if self.install_state < InstallState.RUNNING_POSTINSTALL:
            self.progress_view.set_status(_("Running install step"))
        else:
            self.progress_view.set_status(_("Running postinstall step"))
        self.ui.set_body(self.progress_view)

