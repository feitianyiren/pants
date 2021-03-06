# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

from builtins import object

from pants.base.deprecated import deprecated


class ZincLanguageMixin(object):
  """A mixin for subsystems for languages compiled with Zinc."""

  @classmethod
  def register_options(cls, register):
    super(ZincLanguageMixin, cls).register_options(register)
    # NB: This option is fingerprinted because the default value is not included in a target's
    # fingerprint. This also has the effect of invalidating only the relevant tasks: ZincCompile
    # in this case.
    register('--strict-deps', advanced=True, default=False, fingerprint=True, type=bool,
             help='The default for the "strict_deps" argument for targets of this language.')

    register('--fatal-warnings', advanced=True, type=bool,
             fingerprint=True,
             removal_version='1.11.0.dev0',
             removal_hint='Use --compiler-option-sets=fatal_warnings instead of fatal_warnings',
             help='The default for the "fatal_warnings" argument for targets of this language.')

    register('--compiler-option-sets', advanced=True, default=[], type=list,
             fingerprint=True,
             help='The default for the "compiler_option_sets" argument '
                  'for targets of this language.')

    register('--zinc-file-manager', advanced=True, default=True, type=bool,
             fingerprint=True,
             help='Use zinc provided file manager to ensure transactional rollback.')

  @property
  def strict_deps(self):
    """When True, limits compile time deps to those that are directly declared by a target.
    :rtype: bool
    """
    return self.get_options().strict_deps

  @property
  @deprecated('1.11.0.dev0', 'Consume fatal_warnings from compiler_option_sets instead.')
  def fatal_warnings(self):
    """If true, make warnings fatal for targets that do not specify fatal_warnings.
    :rtype: bool
    """
    return self.get_options().fatal_warnings

  @property
  def compiler_option_sets(self):
    """For every element in this list, enable the corresponding flags on compilation
    of targets.
    :rtype: list
    """
    option_sets = self.get_options().compiler_option_sets
    if 'fatal_warnings' not in option_sets and self.get_options().fatal_warnings:
      option_sets.append('fatal_warnings')
    return option_sets

  @property
  def zinc_file_manager(self):
    """If false, the default file manager will be used instead of the zinc provided one.
    :rtype: bool
    """
    return self.get_options().zinc_file_manager
