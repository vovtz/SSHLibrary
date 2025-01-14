#  Copyright 2008-2015 Nokia Networks
#  Copyright 2016-     Robot Framework Foundation
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import ntpath
import time

try:
    import paramiko
except ImportError:
    raise ImportError(
        'Importing Paramiko library failed. '
        'Make sure you have Paramiko installed.'
    )

try:
    import scp
except ImportError:
    raise ImportError(
        'Importing SCP library failed. '
        'Make sure you have SCP installed.'
    )

from .abstractclient import (AbstractShell, AbstractSFTPClient,
                             AbstractSSHClient, AbstractCommand,
                             SSHClientException, SFTPFileInfo)
from .pythonforward import LocalPortForwarding
from .utils import is_bytes, is_list_like, is_unicode


# There doesn't seem to be a simpler way to increase banner timeout
def _custom_start_client(self, *args, **kwargs):
    self.banner_timeout = 45
    self._orig_start_client(*args, **kwargs)


paramiko.transport.Transport._orig_start_client = \
    paramiko.transport.Transport.start_client
paramiko.transport.Transport.start_client = _custom_start_client


# See http://code.google.com/p/robotframework-sshlibrary/issues/detail?id=55
def _custom_log(self, level, msg, *args):
    escape = lambda s: s.replace('%', '%%')
    if is_list_like(msg):
        msg = [escape(m) for m in msg]
    else:
        msg = escape(msg)
    return self._orig_log(level, msg, *args)


paramiko.sftp_client.SFTPClient._orig_log = paramiko.sftp_client.SFTPClient._log
paramiko.sftp_client.SFTPClient._log = _custom_log


class PythonSSHClient(AbstractSSHClient):
    tunnel = None

    def _get_client(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return client

    @staticmethod
    def enable_logging(path):
        paramiko.util.log_to_file(path)
        return True

    @staticmethod
    def _read_ssh_config_host(host):
        ssh_config_file = os.path.expanduser("~/.ssh/config")
        if os.path.exists(ssh_config_file):
            conf = paramiko.SSHConfig()
            with open(ssh_config_file) as f:
                conf.parse(f)
            return conf.lookup(host)['hostname'] if not None else host
        return host

    def _login(self, username, password, allow_agent=False, look_for_keys=False, proxy_cmd=None):
        self.config.host = self._read_ssh_config_host(self.config.host)
        try:
            if proxy_cmd:
                proxy_cmd = paramiko.ProxyCommand(proxy_cmd)
            try:
                self.client.connect(self.config.host, self.config.port, username,
                                    password, look_for_keys=look_for_keys,
                                    allow_agent=look_for_keys,
                                    timeout=float(self.config.timeout), sock=proxy_cmd)
            except paramiko.AuthenticationException as connect_exception:
                try:
                    transport = self.client.get_transport()
                    try:
                        transport.auth_none(username)
                    except:
                        pass
                    transport.auth_password(username, password)
                except Exception as user_pass_auth_exception:
                    raise SSHClientException(
                        'User/password authentication failed\nCauses: {}\n{}'.format(connect_exception,
                                                                                     user_pass_auth_exception))
        except paramiko.AuthenticationException as auth_exception:
            raise SSHClientException('Login failed\nCause: {}'.format(auth_exception))

    def _login_with_public_key(self, username, key_file, password, allow_agent, look_for_keys, proxy_cmd=None):
        self.config.host = self._read_ssh_config_host(self.config.host)
        try:
            if proxy_cmd:
                proxy_cmd = paramiko.ProxyCommand(proxy_cmd)
            self.client.connect(self.config.host, self.config.port, username,
                                password, key_filename=key_file,
                                allow_agent=allow_agent,
                                look_for_keys=look_for_keys,
                                timeout=float(self.config.timeout),
                                sock=proxy_cmd)
        except paramiko.AuthenticationException as auth_exception:
            try:
                transport = self.client.get_transport()
                try:
                    transport.auth_none(username)
                except:
                    pass
                transport.auth_publickey(username, None)
            except Exception as err:
                raise SSHClientException(
                    'Unable to perform fallback login with user/pass\nCauses: {}\n{}'.format(err, auth_exception))

    def get_banner(self):
        return self.client.get_transport().get_banner()

    @staticmethod
    def get_banner_without_login(host, port=22):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(str(host), int(port), username="bad-username", password="bad-password")
        except paramiko.AuthenticationException:
            return client.get_transport().get_banner()
        except Exception:
            raise SSHClientException('Unable to connect to port {} on {}'.format(port, host))

    def _start_command(self, command, sudo=False, sudo_password=None, invoke_subsystem=False, forward_agent=False):
        cmd = RemoteCommand(command, self.config.encoding)
        transport = self.client.get_transport()
        if not transport:
            raise AssertionError("Connection not open")
        new_shell = transport.open_session(timeout=float(self.config.timeout))

        if forward_agent:
            paramiko.agent.AgentRequestHandler(new_shell)

        cmd.run_in(new_shell, sudo, sudo_password, invoke_subsystem)
        return cmd

    def _create_sftp_client(self):
        return SFTPClient(self.client, self.config.encoding)

    def _create_scp_transfer_client(self):
        return SCPTransferClient(self.client, self.config.encoding)

    def _create_scp_all_client(self):
        return SCPClient(self.client)

    def _create_shell(self):
        return Shell(self.client, self.config.term_type,
                     self.config.width, self.config.height)

    def create_local_ssh_tunnel(self, local_port, remote_host, remote_port, bind_address):
        self._create_local_port_forwarder(local_port, remote_host, remote_port, bind_address)

    def _create_local_port_forwarder(self, local_port, remote_host, remote_port, bind_address):
        transport = self.client.get_transport()
        if not transport:
            raise AssertionError("Connection not open")
        self.tunnel = LocalPortForwarding(int(remote_port), remote_host, transport, bind_address)
        self.tunnel.forward(int(local_port))

    def close(self):
        if self.tunnel:
            self.tunnel.close()
        return super(PythonSSHClient, self).close()


class Shell(AbstractShell):

    def __init__(self, client, term_type, term_width, term_height):
        try:
            self._shell = client.invoke_shell(term_type, term_width, term_height)
        except AttributeError:
            raise RuntimeError('Cannot open session, you need to establish a connection first.')

    def read(self):
        data = b''
        while self._output_available():
            data += self._shell.recv(4096)
        return data

    def read_byte(self):
        if self._output_available():
            return self._shell.recv(1)
        return b''

    def resize(self, width, height):
        self._shell.resize_pty(width=width, height=height)

    def _output_available(self):
        return self._shell.recv_ready()

    def write(self, text):
        self._shell.sendall(text)


class SFTPClient(AbstractSFTPClient):

    def __init__(self, ssh_client, encoding):
        self.ssh_client = ssh_client
        self._client = ssh_client.open_sftp()
        super(SFTPClient, self).__init__(encoding)

    def _list(self, path):
        path = path.encode(self._encoding)
        for item in self._client.listdir_attr(path):
            filename = item.filename
            if is_bytes(filename):
                filename = filename.decode(self._encoding)
            yield SFTPFileInfo(filename, item.st_mode)

    def _stat(self, path):
        path = path.encode(self._encoding)
        attributes = self._client.stat(path)
        return SFTPFileInfo('', attributes.st_mode)

    def _create_missing_remote_path(self, path, mode):
        if is_unicode(path):
            path = path.encode(self._encoding)
        return super(SFTPClient, self)._create_missing_remote_path(path, mode)

    def _create_remote_file(self, destination, mode):
        file_exists = self.is_file(destination)
        destination = destination.encode(self._encoding)
        remote_file = self._client.file(destination, 'wb')
        remote_file.set_pipelined(True)
        if not file_exists and mode:
            self._client.chmod(destination, mode)
        return remote_file

    def _write_to_remote_file(self, remote_file, data, position):
        remote_file.write(data)

    def _close_remote_file(self, remote_file):
        remote_file.close()

    def _get_file(self, remote_path, local_path):
        remote_path = remote_path.encode(self._encoding)
        self._client.get(remote_path, local_path)

    def _absolute_path(self, path):
        if not self._is_windows_path(path):
            path = self._client.normalize(path)
        if is_bytes(path):
            path = path.decode(self._encoding)
        return path

    def _is_windows_path(self, path):
        return bool(ntpath.splitdrive(path)[0])

    def _readlink(self, path):
        return self._client.readlink(path)


class SCPClient(object):
    def __init__(self, ssh_client):
        self._scp_client = scp.SCPClient(ssh_client.get_transport())

    def put_file(self, source, destination, *args):
        self._scp_client.put(source, destination)

    def get_file(self, source, destination, *args):
        self._scp_client.get(source, destination)

    def put_directory(self, source, destination, *args):
        self._scp_client.put(source, destination, True)

    def get_directory(self, source, destination, *args):
        self._scp_client.get(source, destination, True)


class SCPTransferClient(SFTPClient):

    def __init__(self, ssh_client, encoding):
        self._scp_client = scp.SCPClient(ssh_client.get_transport())
        super(SCPTransferClient, self).__init__(ssh_client, encoding)

    def _put_file(self, source, destination, mode, newline, path_separator):
        self._create_remote_file(destination, mode)
        self._scp_client.put(source, destination)

    def _get_file(self, remote_path, local_path):
        self._scp_client.get(remote_path, local_path)


class RemoteCommand(AbstractCommand):

    def read_outputs(self, timeout=None):
        stderr, stdout = self._receive_stdout_and_stderr(timeout)
        rc = self._shell.recv_exit_status()
        self._shell.close()
        return stdout, stderr, rc

    def _receive_stdout_and_stderr(self, timeout=None):
        stdout_filebuffer = self._shell.makefile('rb', -1)
        stderr_filebuffer = self._shell.makefile_stderr('rb', -1)
        stdouts = []
        stderrs = []
        while self._shell_open():
            self._flush_stdout_and_stderr(stderr_filebuffer, stderrs, stdout_filebuffer, stdouts, timeout)
            time.sleep(0.01)  # lets not be so busy
        stdout = (b''.join(stdouts) + stdout_filebuffer.read()).decode(self._encoding)
        stderr = (b''.join(stderrs) + stderr_filebuffer.read()).decode(self._encoding)
        return stderr, stdout

    def _flush_stdout_and_stderr(self, stderr_filebuffer, stderrs, stdout_filebuffer, stdouts, timeout=None):
        if timeout:
            self._shell.status_event.wait(timeout)
            if not self._shell.status_event.isSet():
                raise SSHClientException('Timed out in {} seconds'.format(int(timeout)))

        if self._shell.recv_ready():
            stdouts.append(stdout_filebuffer.read(len(self._shell.in_buffer)))
        if self._shell.recv_stderr_ready():
            stderrs.append(stderr_filebuffer.read(len(self._shell.in_stderr_buffer)))

    def _shell_open(self):
        return not (self._shell.closed or
                    self._shell.eof_received or
                    self._shell.eof_sent or
                    not self._shell.active)

    def _execute(self):
        self._shell.exec_command(self._command)

    def _execute_with_sudo(self, sudo_password=None):
        command = 'sudo ' + self._command.decode(self._encoding)
        self._shell.get_pty()
        if sudo_password is None:
            self._shell.exec_command(command)
        else:
            self._shell.exec_command('echo %s | sudo --stdin --prompt "" %s' % (sudo_password, command))

    def _invoke(self):
        self._shell.invoke_subsystem(self._command)
