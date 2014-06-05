# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (nested_scopes, generators, division, absolute_import, with_statement,
                        print_function, unicode_literals)

import os
from textwrap import dedent

from twitter.common.lang import Compatibility

from pants.backend.jvm.targets.artifact import Artifact
from pants.backend.jvm.targets.jar_dependency import JarDependency
from pants.backend.jvm.targets.jar_library import JarLibrary
from pants.backend.jvm.targets.java_library import JavaLibrary
from pants.backend.jvm.targets.scala_library import ScalaLibrary
from pants.base.address import BuildFileAddress
from pants.base.build_file import BuildFile
from pants.base.build_file_parser import BuildFileParser
from pants.base.config import Config
from pants.base.exceptions import TargetDefinitionException

from pants_test.base_test import BaseTest

import pytest


class BuildFileParserTest(BaseTest):
  def setUp(self):
    super(BuildFileParserTest, self).setUp()

  def test_target_proxy_exceptions(self):
    self.add_to_build_file('a/BUILD', 'dependencies()')
    build_file_a = BuildFile(self.build_root, 'a/BUILD')

    with pytest.raises(ValueError):
      self.build_file_parser.parse_build_file(build_file_a)

    self.add_to_build_file('b/BUILD', 'dependencies(name="foo", "bad_arg")')
    build_file_b = BuildFile(self.build_root, 'b/BUILD')
    with pytest.raises(SyntaxError):
      self.build_file_parser.parse_build_file(build_file_b)

    self.add_to_build_file('c/BUILD', 'dependencies(name="foo", build_file="bad")')
    build_file_c = BuildFile(self.build_root, 'c/BUILD')
    with pytest.raises(ValueError):
      self.build_file_parser.parse_build_file(build_file_c)

    self.add_to_build_file('d/BUILD', dedent(
      '''
      dependencies(name="foo",
        dependencies=[
          object(),
        ]
      )
      '''
    ))
    build_file_d = BuildFile(self.build_root, 'd/BUILD')
    with pytest.raises(TargetDefinitionException):
      self.build_file_parser.parse_build_file(build_file_d)

  def test_noop_parse(self):
    with self.workspace('BUILD') as root_dir:
      parser = BuildFileParser(root_dir=root_dir)
      build_file = BuildFile(root_dir, '')
      parser.parse_build_file(build_file)
      registered_proxies = set(parser._target_proxy_by_address.values())
      self.assertEqual(len(registered_proxies), 0)

  def test_trivial_target(self):
    with self.workspace('BUILD') as root_dir:
      def fake_target(*args, **kwargs):
        assert False, "This fake target should never be called in this test!"

      alias_map = {'target_aliases': {'fake': fake_target}}
      self.build_file_parser.register_alias_groups(alias_map=alias_map)
      with open(os.path.join(root_dir, 'BUILD'), 'w') as build:
        build.write('''fake(name='foozle')''')

      build_file = BuildFile(root_dir, 'BUILD')
      self.build_file_parser.parse_build_file(build_file)
      registered_proxies = set(self.build_file_parser._target_proxy_by_address.values())

    self.assertEqual(len(registered_proxies), 1)
    proxy = registered_proxies.pop()
    self.assertEqual(proxy.name, 'foozle')
    self.assertEqual(proxy.address, BuildFileAddress(build_file, 'foozle'))
    self.assertEqual(proxy.target_type, fake_target)

  def test_exposed_object(self):
    with self.workspace('BUILD') as root_dir:
      alias_map = {'exposed_objects': {'fake_object': object()}}
      self.build_file_parser.register_alias_groups(alias_map=alias_map)

      with open(os.path.join(root_dir, 'BUILD'), 'w') as build:
        build.write('''fake_object''')

      build_file = BuildFile(root_dir, 'BUILD')
      self.build_file_parser.parse_build_file(build_file)
      registered_proxies = set(self.build_file_parser._target_proxy_by_address.values())

    self.assertEqual(len(registered_proxies), 0)

  def test_path_relative_util(self):
    with self.workspace('a/b/c/BUILD') as root_dir:
      def path_relative_util(foozle, rel_path):
        self.assertEqual(rel_path, 'a/b/c')
      alias_map = {'partial_path_relative_utils': {'fake_util': path_relative_util}}
      self.build_file_parser.register_alias_groups(alias_map=alias_map)

      with open(os.path.join(root_dir, 'a/b/c/BUILD'), 'w') as build:
        build.write('''fake_util("baz")''')

      build_file = BuildFile(root_dir, 'a/b/c/BUILD')
      self.build_file_parser.parse_build_file(build_file)
      registered_proxies = set(self.build_file_parser._target_proxy_by_address.values())

    self.assertEqual(len(registered_proxies), 0)

  # Todo (pl): The method "_populate_target_proxy_transitive_closure_for_spec" does not exists.
  # def test_transitive_closure_spec(self):
  #   with self.workspace('./BUILD', 'a/BUILD', 'a/b/BUILD') as root_dir:
  #     with open(os.path.join(root_dir, './BUILD'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="foo",
  #              dependencies=[
  #                'a',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, 'a/BUILD'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="a",
  #              dependencies=[
  #                'a/b:bat',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, 'a/b/BUILD'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="bat")
  #       '''))
  #
  #     parser = BuildFileParser(root_dir=root_dir,
  #                              exposed_objects={},
  #                              path_relative_utils={},
  #                              target_alias_map={'fake': Target})
  #
  #     parser._populate_target_proxy_transitive_closure_for_spec(':foo')
  #     self.assertEqual(len(parser._target_proxy_by_address), 3)
  #
  # def test_sibling_build_files(self):
  #   with self.workspace('./BUILD', './BUILD.foo', './BUILD.bar') as root_dir:
  #     with open(os.path.join(root_dir, './BUILD'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="base",
  #              dependencies=[
  #                ':foo',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, './BUILD.foo'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="foo",
  #              dependencies=[
  #                ':bat',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, './BUILD.bar'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="bat")
  #       '''))
  #
  #     parser = BuildFileParser(root_dir=root_dir,
  #                              exposed_objects={},
  #                              path_relative_utils={},
  #                              target_alias_map={'fake': Target})
  #
  #     bar_build_file = BuildFile(root_dir, 'BUILD.bar')
  #     base_build_file = BuildFile(root_dir, 'BUILD')
  #     foo_build_file = BuildFile(root_dir, 'BUILD.foo')
  #     parser.parse_build_file_family(bar_build_file)
  #     addresses = parser._target_proxy_by_address.keys()
  #     self.assertEqual(set([bar_build_file, base_build_file, foo_build_file]),
  #                      set([address.build_file for address in addresses]))
  #     self.assertEqual(set([':base', ':foo', ':bat']),
  #                      set([address.spec for address in addresses]))
  #
  #   # This workspace is malformed, you can't shadow a name in a sibling BUILD file
  #   with self.workspace('./BUILD', './BUILD.foo', './BUILD.bar') as root_dir:
  #     with open(os.path.join(root_dir, './BUILD'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="base",
  #              dependencies=[
  #                ':foo',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, './BUILD.foo'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="foo",
  #              dependencies=[
  #                ':bat',
  #              ])
  #       '''))
  #
  #     with open(os.path.join(root_dir, './BUILD.bar'), 'w') as build:
  #       build.write(dedent('''
  #         fake(name="base")
  #       '''))
  #
  #     parser = BuildFileParser(root_dir=root_dir,
  #                              exposed_objects={},
  #                              path_relative_utils={},
  #                              target_alias_map={'fake': FakeTarget})
  #     with pytest.raises(AssertionError):
  #       parser.populate_target_proxy_transitive_closure_for_spec(':base')
  #
  def test_target_creation(self):
    contents = dedent('''
                 create_java_libraries(base_name="create-java-libraries",
                                       sources=[],
                                       provides_java_name="test-java",
                                       provides_scala_name="test-scala")
                 make_lib("com.foo.test", "does_not_exists", "1.0")
               ''')
    self.create_file('3rdparty/BUILD', contents)
    alias_map = {'target_aliases': {'artifact': Artifact,
                                    'jar_library': JarLibrary,
                                    'java_library': JavaLibrary,
                                    'scala_library': ScalaLibrary},
                   'target_creation_utils': {'make_lib': make_lib,
                                             'create_java_libraries': create_java_libraries},
                   'exposed_objects': {'artifact': Artifact,
                                       'jar': JarDependency}
                  }

    self.build_file_parser.register_alias_groups(alias_map=alias_map)
    build_file = BuildFile(self.build_root, '3rdparty/BUILD')
    self.build_file_parser.parse_build_file(build_file)
    registered_proxies = set(self.build_file_parser._target_proxy_by_address.values())
    self.assertEqual(len(registered_proxies), 3)


def make_lib(org, name, rev, alias=None, sources=True, alias_map=None):
  dep = alias_map['jar'](org=org, name=name, rev=rev)
  if sources:
    dep.with_sources()
  alias_map['jar_library'](name=name, jars=[dep])
  if alias:
    alias_map['jar_library'](name=alias, jars=[dep])


def create_java_libraries(
  base_name,
  sources,
  dependency_roots=None,
  org='com.twitter',
  provides_java_name=None,
  provides_scala_name=None,
  alias_map=None):
  if not isinstance(base_name, Compatibility.string):
    raise ValueError('create_java_libraries base_name must be a string: %s' % base_name)
  config = Config.load()
  def provides_artifact(provides_name):
    if provides_name is None:
      return None
    jvm_repo = config.get('create_java_libraries', 'jvm_repo',
                          default='pants-support/ivy:gem-internal')
    return Artifact(org=org,
                    name=provides_name,
                    repo=jvm_repo)
  alias_map['java_library'](
    name='%s-java' % base_name,
    sources=sources,
    dependencies=[],
    provides=provides_artifact(provides_java_name))

  alias_map['scala_library'](
    name='%s-scala' % base_name,
    sources=sources,
    dependencies=[],
    provides=provides_artifact(provides_scala_name))
