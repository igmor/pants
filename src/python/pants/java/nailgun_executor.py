# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (nested_scopes, generators, division, absolute_import, with_statement,
                        print_function, unicode_literals)

import hashlib
import os
import re
import time

from collections import namedtuple

import psutil

# TODO: Once we integrate standard logging into our reporting framework, we  can consider making
#  some of the log.debug() below into log.info(). Right now it just looks wrong on the console.
from twitter.common import log
from twitter.common.collections import maybe_list
from twitter.common.lang import Compatibility

from pants.base.build_environment import get_buildroot
from pants.java.executor import Executor, SubprocessExecutor
from pants.java.nailgun_client import NailgunClient
from pants.util.dirutil import safe_open


class NailgunExecutor(Executor):
  """Executes java programs by launching them in nailgun server.

  If a nailgun is not available for a given set of jvm args and classpath, one is launched and
  re-used for the given jvm args and classpath on subsequent runs.
  """

  class Endpoint(namedtuple('Endpoint', ['fingerprint', 'pid', 'port'])):
    """The coordinates for a nailgun server controlled by NailgunExecutor."""

    @classmethod
    def parse(cls, endpoint):
      """Parses an endpoint from a string of the form fingerprint:pid:port"""
      components = endpoint.split(':')
      if len(components) != 3:
        raise ValueError('Invalid endpoint spec %s' % endpoint)
      fingerprint, pid, port = components
      return cls(fingerprint, int(pid), int(port))

  # Used to identify we own a given java nailgun server
  _PANTS_NG_ARG_PREFIX = b'-Dpants.buildroot'
  _PANTS_NG_ARG = b'%s=%s' % (_PANTS_NG_ARG_PREFIX, get_buildroot())

  _PANTS_FINGERPRINT_ARG_PREFIX = b'-Dpants.nailgun.fingerprint='

  @staticmethod
  def _check_pid(pid):
    try:
      os.kill(pid, 0)
      return True
    except OSError:
      return False

  @staticmethod
  def create_owner_arg(workdir):
    # Currently the owner is identified via the full path to the workdir.
    return b'-Dpants.nailgun.owner=%s' % workdir

  @classmethod
  def _create_fingerprint_arg(cls, fingerprint):
    return cls._PANTS_FINGERPRINT_ARG_PREFIX + fingerprint

  @classmethod
  def parse_fingerprint_arg(cls, args):
    for arg in args:
      components = arg.split(cls._PANTS_FINGERPRINT_ARG_PREFIX)
      if len(components) == 2 and components[0] == '':
        return components[1]
    return None

  @staticmethod
  def _fingerprint(jvm_args, classpath):
    digest = hashlib.sha1()
    digest.update(''.join(sorted(jvm_args)))
    digest.update(''.join(sorted(classpath)))  # TODO(John Sirois): hash classpath contents?
    return digest.hexdigest()

  @staticmethod
  def _log_kill(pid, port=None, logger=None):
    logger = logger or log.info
    logger('killing ng server @ pid:%d%s' % (pid, ' port:%d' % port if port else ''))

  @classmethod
  def _find_ngs(cls, everywhere=False):
    def cmdline_matches(cmdline):
      if everywhere:
        return any(filter(lambda arg: arg.startswith(cls._PANTS_NG_ARG_PREFIX), cmdline))
      else:
        return cls._PANTS_NG_ARG in cmdline

    for proc in psutil.process_iter():
      try:
        if b'java' == proc.name and cmdline_matches(proc.cmdline):
          yield proc
      except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass

  @classmethod
  def killall(cls, logger=None, everywhere=False):
    """Kills all nailgun servers started by pants.

    :param bool everywhere: If ``True`` Kills all pants-started nailguns on this machine; otherwise
      restricts the nailguns killed to those started for the current build root.
    """
    success = True
    for proc in cls._find_ngs(everywhere=everywhere):
      try:
        cls._log_kill(proc.pid, logger=logger)
        proc.kill()
      except (psutil.AccessDenied, psutil.NoSuchProcess):
        success = False
    return success

  @staticmethod
  def _find_ng_listen_port(proc):
    for connection in proc.get_connections(kind=b'tcp'):
      if connection.status == b'LISTEN':
        host, port = connection.laddr
        return port
    return None

  @classmethod
  def _find(cls, workdir):
    owner_arg = cls.create_owner_arg(workdir)
    for proc in cls._find_ngs(everywhere=False):
      try:
        if owner_arg in proc.cmdline:
          fingerprint = cls.parse_fingerprint_arg(proc.cmdline)
          port = cls._find_ng_listen_port(proc)
          if fingerprint and port:
            return cls.Endpoint(fingerprint, proc.pid, port)
      except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    return None

  def __init__(self, workdir, nailgun_classpath, distribution=None, ins=None):
    super(NailgunExecutor, self).__init__(distribution=distribution)

    self._nailgun_classpath = maybe_list(nailgun_classpath)

    if not isinstance(workdir, Compatibility.string):
      raise ValueError('Workdir must be a path string, given %s' % workdir)

    self._workdir = workdir

    self._ng_out = os.path.join(workdir, 'stdout')
    self._ng_err = os.path.join(workdir, 'stderr')

    self._ins = ins

  def _runner(self, classpath, main, jvm_options, args):
    command = self._create_command(classpath, main, jvm_options, args)

    class Runner(self.Runner):
      @property
      def executor(this):
        return self

      @property
      def cmd(this):
        return ' '.join(command)

      def run(this, stdout=None, stderr=None):
        nailgun = self._get_nailgun_client(jvm_options, classpath, stdout, stderr)
        try:
          log.debug('Executing via %s: %s' % (nailgun, this.cmd))
          return nailgun(main, *args)
        except nailgun.NailgunError as e:
          self.kill()
          raise self.Error('Problem launching via %s command %s %s: %s'
                           % (nailgun, main, ' '.join(args), e))

    return Runner()

  def kill(self):
    """Kills the nailgun server owned by this executor if its currently running."""

    endpoint = self._get_nailgun_endpoint()
    if endpoint:
      self._log_kill(endpoint.pid, endpoint.port)
      try:
        os.kill(endpoint.pid, 9)
      except OSError:
        pass

  def _get_nailgun_endpoint(self):
    endpoint = self._find(self._workdir)
    if endpoint:
      log.debug('Found ng server with fingerprint %s @ pid:%d port:%d' % endpoint)
    return endpoint

  def _get_nailgun_client(self, jvm_args, classpath, stdout, stderr):
    classpath = self._nailgun_classpath + classpath
    new_fingerprint = self._fingerprint(jvm_args, classpath)

    endpoint = self._get_nailgun_endpoint()
    running = endpoint and self._check_pid(endpoint.pid)
    updated = endpoint and endpoint.fingerprint != new_fingerprint
    if running and not updated:
      return self._create_ngclient(endpoint.port, stdout, stderr)
    else:
      if running and updated:
        log.debug('Killing ng server with fingerprint %s @ pid:%d port:%d' % endpoint)
        self.kill()
      return self._spawn_nailgun_server(new_fingerprint, jvm_args, classpath, stdout, stderr)

  # 'NGServer started on 127.0.0.1, port 53785.'
  _PARSE_NG_PORT = re.compile('.*\s+port\s+(\d+)\.$')

  def _parse_nailgun_port(self, line):
    match = self._PARSE_NG_PORT.match(line)
    if not match:
      raise NailgunClient.NailgunError('Failed to determine spawned ng port from response'
                                       ' line: %s' % line)
    return int(match.group(1))

  def _await_nailgun_server(self, stdout, stderr):
    nailgun_timeout_seconds = 5
    max_socket_connect_attempts = 10
    nailgun = None
    port_parse_start = time.time()
    with safe_open(self._ng_out, 'r') as ng_out:
      while not nailgun:
        started = ng_out.readline()
        if started:
          port = self._parse_nailgun_port(started)
          nailgun = self._create_ngclient(port, stdout, stderr)
          log.debug('Detected ng server up on port %d' % port)
        elif time.time() - port_parse_start > nailgun_timeout_seconds:
          raise NailgunClient.NailgunError('Failed to read ng output after'
                                           ' %s seconds' % nailgun_timeout_seconds)

    attempt = 0
    while nailgun:
      sock = nailgun.try_connect()
      if sock:
        sock.close()
        endpoint = self._get_nailgun_endpoint()
        if endpoint:
          log.debug('Connected to ng server with fingerprint %s pid: %d @ port: %d' % endpoint)
        else:
          raise NailgunClient.NailgunError('Failed to connect to ng server.')
        return nailgun
      elif attempt > max_socket_connect_attempts:
        raise nailgun.NailgunError('Failed to connect to ng output after %d connect attempts'
                                   % max_socket_connect_attempts)
      attempt += 1
      log.debug('Failed to connect on attempt %d' % attempt)
      time.sleep(0.1)

  def _create_ngclient(self, port, stdout, stderr):
    return NailgunClient(port=port, ins=self._ins, out=stdout, err=stderr, workdir=get_buildroot())

  def _spawn_nailgun_server(self, fingerprint, jvm_args, classpath, stdout, stderr):
    log.debug('No ng server found with fingerprint %s, spawning...' % fingerprint)

    with safe_open(self._ng_out, 'w'):
      pass  # truncate

    pid = os.fork()
    if pid != 0:
      # In the parent tine - block on ng being up for connections
      return self._await_nailgun_server(stdout, stderr)

    os.setsid()
    in_fd = open('/dev/null', 'r')
    out_fd = safe_open(self._ng_out, 'w')
    err_fd = safe_open(self._ng_err, 'w')

    java = SubprocessExecutor(self._distribution)

    jvm_args = jvm_args + [self._PANTS_NG_ARG,
                           self.create_owner_arg(self._workdir),
                           self._create_fingerprint_arg(fingerprint)]

    process = java.spawn(classpath=classpath,
                         main='com.martiansoftware.nailgun.NGServer',
                         jvm_options=jvm_args,
                         args=[':0'],
                         stdin=in_fd,
                         stdout=out_fd,
                         stderr=err_fd,
                         close_fds=True)

    log.debug('Spawned ng server with fingerprint %s @ %d' % (fingerprint, process.pid))
    # Prevents finally blocks and atexit handlers from being executed, unlike sys.exit(). We
    # don't want to execute finally blocks because we might, e.g., clean up tempfiles that the
    # parent still needs.
    os._exit(0)

  def __str__(self):
    return 'NailgunExecutor(%s, server=%s)' % (self._distribution, self._get_nailgun_endpoint())
