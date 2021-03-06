# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from errno import ENOENT
from glob import glob
import os
from os.path import abspath, basename, dirname, expanduser, expandvars, isdir, join, normcase
import re
import sys
from tempfile import NamedTemporaryFile

from . import CONDA_PACKAGE_ROOT, CondaError
from .base.context import ROOT_ENV_NAME, context, locate_prefix_by_name

try:
    from cytoolz.itertoolz import concatv, drop
except ImportError:  # pragma: no cover
    from ._vendor.toolz.itertoolz import concatv, drop  # NOQA


class _Activator(object):
    # Activate and deactivate have three tasks
    #   1. Set and unset environment variables
    #   2. Execute/source activate.d/deactivate.d scripts
    #   3. Update the command prompt
    #
    # Shells should also use 'reactivate' following conda's install, update, and
    #   remove/uninstall commands.
    #
    # All core logic is in build_activate() or build_deactivate(), and is independent of
    # shell type.  Each returns a map containing the keys:
    #   export_vars
    #   unset_var
    #   activate_scripts
    #   deactivate_scripts
    #
    # The value of the CONDA_PROMPT_MODIFIER environment variable holds conda's contribution
    #   to the command prompt.
    #
    # To implement support for a new shell, ideally one would only need to add shell-specific
    # information to the __init__ method of this class.

    # The following instance variables must be defined by each implementation.
    pathsep_join = None
    sep = None
    path_conversion = None
    script_extension = None
    tempfile_extension = None  # None means write instructions to stdout rather than a temp file
    command_join = None

    unset_var_tmpl = None
    export_var_tmpl = None
    set_var_tmpl = None
    run_script_tmpl = None

    hook_source_path = None

    def __init__(self, arguments=None):
        self._raw_arguments = arguments

        if PY2:
            self.environ = {ensure_fs_path_encoding(k): ensure_fs_path_encoding(v)
                            for k, v in iteritems(os.environ)}
        else:
            self.environ = os.environ.copy()

    def _finalize(self, commands, ext):
        commands = concatv(commands, ('',))  # add terminating newline
        if ext is None:
            return self.command_join.join(commands)
        elif ext:
            with NamedTemporaryFile('w+b', suffix=ext, delete=False) as tf:
                # the default mode is 'w+b', and universal new lines don't work in that mode
                # command_join should account for that
                tf.write(ensure_binary(self.command_join.join(commands)))
            return tf.name
        else:
            raise NotImplementedError()

    def activate(self):
        if self.stack:
            builder_result = self.build_stack(self.env_name_or_prefix)
        else:
            builder_result = self.build_activate(self.env_name_or_prefix)
        return self._finalize(self._yield_commands(builder_result), self.tempfile_extension)

    def deactivate(self):
        return self._finalize(self._yield_commands(self.build_deactivate()),
                              self.tempfile_extension)

    def reactivate(self):
        return self._finalize(self._yield_commands(self.build_reactivate()),
                              self.tempfile_extension)

    def hook(self, auto_activate_base=None):
        builder = []
        builder.append(self._hook_preamble())
        with open(self.hook_source_path) as fsrc:
            builder.append(fsrc.read())
        if auto_activate_base is None and context.auto_activate_base or auto_activate_base:
            builder.append("conda activate base\n")
        return "\n".join(builder)

    def execute(self):
        # return value meant to be written to stdout
        self._parse_and_set_args(self._raw_arguments)
        return getattr(self, self.command)()

    def _hook_preamble(self):
        # must be implemented in subclass
        raise NotImplementedError()

    def _parse_and_set_args(self, arguments):
        # the first index of arguments MUST be either activate, deactivate, or reactivate
        if arguments is None:
            from .exceptions import ArgumentError
            raise ArgumentError("'activate', 'deactivate', or 'reactivate' command must be given")

        command = arguments[0]
        arguments = tuple(drop(1, arguments))
        help_flags = ('-h', '--help', '/?')
        non_help_args = tuple(arg for arg in arguments if arg not in help_flags)
        help_requested = len(arguments) != len(non_help_args)
        remainder_args = list(arg for arg in non_help_args if arg and arg != command)

        if not command:
            from .exceptions import ArgumentError
            raise ArgumentError("'activate', 'deactivate', 'hook', or 'reactivate' "
                                "command must be given")
        elif help_requested:
            from .exceptions import ActivateHelp, DeactivateHelp, GenericHelp
            help_classes = {
                'activate': ActivateHelp(),
                'deactivate': DeactivateHelp(),
                'hook': GenericHelp('hook'),
                'reactivate': GenericHelp('reactivate'),
            }
            raise help_classes[command]
        elif command not in ('activate', 'deactivate', 'reactivate', 'hook'):
            from .exceptions import ArgumentError
            raise ArgumentError("invalid command '%s'" % command)

        if command == 'activate':
            try:
                stack_idx = remainder_args.index('--stack')
            except ValueError:
                self.stack = False
            else:
                del remainder_args[stack_idx]
                self.stack = True
            if len(remainder_args) > 1:
                from .exceptions import ArgumentError
                raise ArgumentError(command + ' does not accept more than one argument:\n'
                                    + str(remainder_args) + '\n')
            self.env_name_or_prefix = remainder_args and remainder_args[0] or 'base'

        else:
            if remainder_args:
                from .exceptions import ArgumentError
                raise ArgumentError('%s does not accept arguments\nremainder_args: %s\n'
                                    % (command, remainder_args))

        self.command = command

    def _yield_commands(self, cmds_dict):
        for script in cmds_dict.get('deactivate_scripts', ()):
            yield self.run_script_tmpl % script

        for key in sorted(cmds_dict.get('unset_vars', ())):
            yield self.unset_var_tmpl % key

        for key, value in sorted(iteritems(cmds_dict.get('set_vars', {}))):
            yield self.set_var_tmpl % (key, value)

        for key, value in sorted(iteritems(cmds_dict.get('export_vars', {}))):
            yield self.export_var_tmpl % (key, value)

        for script in cmds_dict.get('activate_scripts', ()):
            yield self.run_script_tmpl % script

    def build_activate(self, env_name_or_prefix):
        return self._build_activate_stack(env_name_or_prefix, False)

    def build_stack(self, env_name_or_prefix):
        return self._build_activate_stack(env_name_or_prefix, True)

    def _build_activate_stack(self, env_name_or_prefix, stack):
        if re.search(r'\\|/', env_name_or_prefix):
            prefix = expand(env_name_or_prefix)
            if not isdir(join(prefix, 'conda-meta')):
                from .exceptions import EnvironmentLocationNotFound
                raise EnvironmentLocationNotFound(prefix)
        elif env_name_or_prefix in (ROOT_ENV_NAME, 'root'):
            prefix = context.root_prefix
        else:
            prefix = locate_prefix_by_name(env_name_or_prefix)

        # query environment
        old_conda_shlvl = int(self.environ.get('CONDA_SHLVL', '').strip() or 0)
        new_conda_shlvl = old_conda_shlvl + 1
        old_conda_prefix = self.environ.get('CONDA_PREFIX')

        if old_conda_prefix == prefix and old_conda_shlvl > 0:
            return self.build_reactivate()

        activate_scripts = self._get_activate_scripts(prefix)
        conda_default_env = self._default_env(prefix)
        conda_prompt_modifier = self._prompt_modifier(prefix, conda_default_env)

        if old_conda_shlvl == 0:
            new_path = self.pathsep_join(self._add_prefix_to_path(prefix))
            conda_python_exe = self.path_conversion(sys.executable)
            conda_exe = self.path_conversion(context.conda_exe)
            export_vars = {
                'CONDA_PYTHON_EXE': conda_python_exe,
                'CONDA_EXE': conda_exe,
                'PATH': new_path,
                'CONDA_PREFIX': prefix,
                'CONDA_SHLVL': new_conda_shlvl,
                'CONDA_DEFAULT_ENV': conda_default_env,
                'CONDA_PROMPT_MODIFIER': conda_prompt_modifier,
            }
            deactivate_scripts = ()
        else:
            if self.environ.get('CONDA_PREFIX_%s' % (old_conda_shlvl - 1)) == prefix:
                # in this case, user is attempting to activate the previous environment,
                #  i.e. step back down
                return self.build_deactivate()
            if stack:
                new_path = self.pathsep_join(self._add_prefix_to_path(prefix))
                export_vars = {
                    'PATH': new_path,
                    'CONDA_PREFIX': prefix,
                    'CONDA_PREFIX_%d' % old_conda_shlvl: old_conda_prefix,
                    'CONDA_SHLVL': new_conda_shlvl,
                    'CONDA_DEFAULT_ENV': conda_default_env,
                    'CONDA_PROMPT_MODIFIER': conda_prompt_modifier,
                    'CONDA_STACKED_%d' % new_conda_shlvl: 'true',
                }
                deactivate_scripts = ()
            else:
                new_path = self.pathsep_join(
                    self._replace_prefix_in_path(old_conda_prefix, prefix)
                )
                export_vars = {
                    'PATH': new_path,
                    'CONDA_PREFIX': prefix,
                    'CONDA_PREFIX_%d' % old_conda_shlvl: old_conda_prefix,
                    'CONDA_SHLVL': new_conda_shlvl,
                    'CONDA_DEFAULT_ENV': conda_default_env,
                    'CONDA_PROMPT_MODIFIER': conda_prompt_modifier,
                }
                deactivate_scripts = self._get_deactivate_scripts(old_conda_prefix)

        set_vars = {}
        if context.changeps1:
            self._update_prompt(set_vars, conda_prompt_modifier)

        self._build_activate_shell_custom(export_vars)

        return {
            'unset_vars': (),
            'set_vars': set_vars,
            'export_vars': export_vars,
            'deactivate_scripts': deactivate_scripts,
            'activate_scripts': activate_scripts,
        }

    def build_deactivate(self):
        # query environment
        old_conda_prefix = self.environ.get('CONDA_PREFIX')
        old_conda_shlvl = int(self.environ.get('CONDA_SHLVL', '').strip() or 0)
        if not old_conda_prefix or old_conda_shlvl < 1:
            # no active environment, so cannot deactivate; do nothing
            return {
                'unset_vars': (),
                'set_vars': {},
                'export_vars': {},
                'deactivate_scripts': (),
                'activate_scripts': (),
            }
        deactivate_scripts = self._get_deactivate_scripts(old_conda_prefix)

        new_conda_shlvl = old_conda_shlvl - 1
        set_vars = {}
        if old_conda_shlvl == 1:
            new_path = self.pathsep_join(self._remove_prefix_from_path(old_conda_prefix))
            conda_prompt_modifier = ''
            unset_vars = (
                'CONDA_PREFIX',
                'CONDA_DEFAULT_ENV',
                'CONDA_PYTHON_EXE',
                'CONDA_PROMPT_MODIFIER',
            )
            export_vars = {
                'PATH': new_path,
                'CONDA_SHLVL': new_conda_shlvl,
            }
            activate_scripts = ()
        else:
            assert old_conda_shlvl > 1
            new_prefix = self.environ.get('CONDA_PREFIX_%d' % new_conda_shlvl)
            conda_default_env = self._default_env(new_prefix)
            conda_prompt_modifier = self._prompt_modifier(new_prefix, conda_default_env)

            old_prefix_stacked = 'CONDA_STACKED_%d' % old_conda_shlvl in self.environ
            if old_prefix_stacked:
                new_path = self.pathsep_join(self._remove_prefix_from_path(old_conda_prefix))
                unset_vars = (
                    'CONDA_PREFIX_%d' % new_conda_shlvl,
                    'CONDA_STACKED_%d' % old_conda_shlvl,
                )
            else:
                new_path = self.pathsep_join(
                    self._replace_prefix_in_path(old_conda_prefix, new_prefix)
                )
                unset_vars = (
                    'CONDA_PREFIX_%d' % new_conda_shlvl,
                )

            export_vars = {
                'PATH': new_path,
                'CONDA_SHLVL': new_conda_shlvl,
                'CONDA_PREFIX': new_prefix,
                'CONDA_DEFAULT_ENV': conda_default_env,
                'CONDA_PROMPT_MODIFIER': conda_prompt_modifier,
            }
            activate_scripts = self._get_activate_scripts(new_prefix)

        if context.changeps1:
            self._update_prompt(set_vars, conda_prompt_modifier)

        return {
            'unset_vars': unset_vars,
            'set_vars': set_vars,
            'export_vars': export_vars,
            'deactivate_scripts': deactivate_scripts,
            'activate_scripts': activate_scripts,
        }

    def build_reactivate(self):
        conda_prefix = self.environ.get('CONDA_PREFIX')
        conda_shlvl = int(self.environ.get('CONDA_SHLVL', '').strip() or 0)
        if not conda_prefix or conda_shlvl < 1:
            # no active environment, so cannot reactivate; do nothing
            return {
                'unset_vars': (),
                'set_vars': {},
                'export_vars': {},
                'deactivate_scripts': (),
                'activate_scripts': (),
            }
        conda_default_env = self.environ.get('CONDA_DEFAULT_ENV', self._default_env(conda_prefix))
        new_path = self.pathsep_join(self._replace_prefix_in_path(conda_prefix, conda_prefix))
        set_vars = {}

        conda_prompt_modifier = self._prompt_modifier(conda_prefix, conda_default_env)
        if context.changeps1:
            self._update_prompt(set_vars, conda_prompt_modifier)

        # environment variables are set only to aid transition from conda 4.3 to conda 4.4
        return {
            'unset_vars': (),
            'set_vars': set_vars,
            'export_vars': {
                'PATH': new_path,
                'CONDA_SHLVL': conda_shlvl,
                'CONDA_PROMPT_MODIFIER': self._prompt_modifier(conda_prefix, conda_default_env),
            },
            'deactivate_scripts': self._get_deactivate_scripts(conda_prefix),
            'activate_scripts': self._get_activate_scripts(conda_prefix),
        }

    def _get_starting_path_list(self):
        path = self.environ['PATH']
        if on_win:
            # On Windows, the Anaconda Python interpreter prepends sys.prefix\Library\bin on
            # startup. It's a hack that allows users to avoid using the correct activation
            # procedure; a hack that needs to go away because it doesn't add all the paths.
            # See: https://github.com/AnacondaRecipes/python-feedstock/blob/master/recipe/0005-Win32-Ensure-Library-bin-is-in-os.environ-PATH.patch  # NOQA
            # But, we now detect if that has happened because:
            #   1. In future we would like to remove this hack and require real activation.
            #   2. We should not assume that the Anaconda Python interpreter is being used.
            path_split = path.split(os.pathsep)
            library_bin = r"%s\Library\bin" % (sys.prefix)
            # ^^^ deliberately the same as: https://github.com/AnacondaRecipes/python-feedstock/blob/8e8aee4e2f4141ecfab082776a00b374c62bb6d6/recipe/0005-Win32-Ensure-Library-bin-is-in-os.environ-PATH.patch#L20  # NOQA
            if paths_equal(path_split[0], library_bin):
                return path_split[1:]
            else:
                return path_split
        else:
            return path.split(os.pathsep)

    @staticmethod
    def _get_path_dirs(prefix):
        if on_win:  # pragma: unix no cover
            yield prefix.rstrip("\\")
            yield join(prefix, 'Library', 'mingw-w64', 'bin')
            yield join(prefix, 'Library', 'usr', 'bin')
            yield join(prefix, 'Library', 'bin')
            yield join(prefix, 'Scripts')
            yield join(prefix, 'bin')
        else:
            yield join(prefix, 'bin')

    def _get_path_dirs2(self, prefix):
        if on_win:  # pragma: unix no cover
            yield prefix
            yield self.sep.join((prefix, 'Library', 'mingw-w64', 'bin'))
            yield self.sep.join((prefix, 'Library', 'usr', 'bin'))
            yield self.sep.join((prefix, 'Library', 'bin'))
            yield self.sep.join((prefix, 'Scripts'))
            yield self.sep.join((prefix, 'bin'))
        else:
            yield self.sep.join((prefix, 'bin'))

    def _add_prefix_to_path(self, prefix, starting_path_dirs=None):
        prefix = self.path_conversion(prefix)
        if starting_path_dirs is None:
            path_list = list(self.path_conversion(self._get_starting_path_list()))
        else:
            path_list = list(self.path_conversion(starting_path_dirs))
        path_list[0:0] = list(self._get_path_dirs2(prefix))
        return tuple(path_list)

    def _remove_prefix_from_path(self, prefix, starting_path_dirs=None):
        return self._replace_prefix_in_path(prefix, None, starting_path_dirs)

    def _replace_prefix_in_path(self, old_prefix, new_prefix, starting_path_dirs=None):
        old_prefix = self.path_conversion(old_prefix)
        new_prefix = self.path_conversion(new_prefix)
        if starting_path_dirs is None:
            path_list = list(self.path_conversion(self._get_starting_path_list()))
        else:
            path_list = list(self.path_conversion(starting_path_dirs))

        def index_of_path(paths, test_path):
            for q, path in enumerate(paths):
                if paths_equal(path, test_path):
                    return q
            return None

        if old_prefix is not None:
            prefix_dirs = tuple(self._get_path_dirs2(old_prefix))
            first_idx = index_of_path(path_list, prefix_dirs[0])
            if first_idx is None:
                first_idx = 0
            else:
                last_idx = index_of_path(path_list, prefix_dirs[-1])
                assert last_idx is not None
                del path_list[first_idx:last_idx + 1]
        else:
            first_idx = 0

        if new_prefix is not None:
            path_list[first_idx:first_idx] = list(self._get_path_dirs2(new_prefix))

        return tuple(path_list)

    def _build_activate_shell_custom(self, export_vars):
        # A method that can be overriden by shell-specific implementations.
        # The signature of this method may change in the future.
        pass

    def _update_prompt(self, set_vars, conda_prompt_modifier):
        pass

    def _default_env(self, prefix):
        if paths_equal(prefix, context.root_prefix):
            return 'base'
        return basename(prefix) if basename(dirname(prefix)) == 'envs' else prefix

    def _prompt_modifier(self, prefix, conda_default_env):
        if context.changeps1:
            return context.env_prompt.format(
                default_env=conda_default_env,
                prefix=prefix,
                name=basename(prefix),
            )
        else:
            return ""

    def _get_activate_scripts(self, prefix):
        return self.path_conversion(sorted(glob(join(
            prefix, 'etc', 'conda', 'activate.d', '*' + self.script_extension
        ))))

    def _get_deactivate_scripts(self, prefix):
        return self.path_conversion(sorted(glob(join(
            prefix, 'etc', 'conda', 'deactivate.d', '*' + self.script_extension
        )), reverse=True))


def expand(path):
    return abspath(expanduser(expandvars(path)))


def ensure_binary(value):
    try:
        return value.encode('utf-8')
    except AttributeError:  # pragma: no cover
        # AttributeError: '<>' object has no attribute 'encode'
        # In this case assume already binary type and do nothing
        return value


def ensure_fs_path_encoding(value):
    try:
        return value.decode(FILESYSTEM_ENCODING)
    except AttributeError:
        return value


def native_path_to_unix(paths):  # pragma: unix no cover
    # on windows, uses cygpath to convert windows native paths to posix paths
    if not on_win:
        return path_identity(paths)
    if paths is None:
        return None
    from subprocess import CalledProcessError, PIPE, Popen
    from shlex import split
    command = 'cygpath --path -f -'

    single_path = isinstance(paths, string_types)
    joined = paths if single_path else ("%s" % os.pathsep).join(paths)

    if hasattr(joined, 'encode'):
        joined = joined.encode('utf-8')

    try:
        p = Popen(split(command), stdin=PIPE, stdout=PIPE, stderr=PIPE)
    except EnvironmentError as e:
        if e.errno != ENOENT:
            raise
        # This code path should (hopefully) never be hit be real conda installs. It's here
        # as a backup for tests run under cmd.exe with cygpath not available.
        def _translation(found_path):  # NOQA
            found = found_path.group(1).replace("\\", "/").replace(":", "").replace("//", "/")
            return "/" + found.rstrip("/")
        joined = ensure_fs_path_encoding(joined)
        stdout = re.sub(
            r'([a-zA-Z]:[\/\\\\]+(?:[^:*?\"<>|;]+[\/\\\\]*)*)',
            _translation,
            joined
        ).replace(";/", ":/").rstrip(";")
    else:
        stdout, stderr = p.communicate(input=joined)
        rc = p.returncode
        if rc != 0 or stderr:
            message = "\n  stdout: %s\n  stderr: %s\n  rc: %s\n" % (stdout, stderr, rc)
            print(message, file=sys.stderr)
            raise CalledProcessError(rc, command, message)
        if hasattr(stdout, 'decode'):
            stdout = stdout.decode('utf-8')
        stdout = stdout.strip()
    final = stdout and stdout.split(':') or ()
    return final[0] if single_path else tuple(final)


def path_identity(paths):
    if isinstance(paths, string_types):
        return paths
    elif paths is None:
        return None
    else:
        return tuple(paths)


def paths_equal(path1, path2):
    if on_win:
        return normcase(abspath(path1)) == normcase(abspath(path2))
    else:
        return abspath(path1) == abspath(path2)


on_win = bool(sys.platform == "win32")
PY2 = sys.version_info[0] == 2
FILESYSTEM_ENCODING = sys.getfilesystemencoding()
if PY2:  # pragma: py3 no cover
    string_types = basestring,  # NOQA
    text_type = unicode  # NOQA

    def iteritems(d, **kw):
        return d.iteritems(**kw)
else:  # pragma: py2 no cover
    string_types = str,
    text_type = str

    def iteritems(d, **kw):
        return iter(d.items(**kw))


class PosixActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = ':'.join
        self.sep = '/'
        self.path_conversion = native_path_to_unix
        self.script_extension = '.sh'
        self.tempfile_extension = None  # write instructions to stdout rather than a temp file
        self.command_join = '\n'

        self.unset_var_tmpl = '\\unset %s'
        self.export_var_tmpl = "\\export %s='%s'"
        self.set_var_tmpl = "%s='%s'"
        self.run_script_tmpl = '\\. "%s"'

        self.hook_source_path = join(CONDA_PACKAGE_ROOT, 'shell', 'etc', 'profile.d', 'conda.sh')

        super(PosixActivator, self).__init__(arguments)

    def _update_prompt(self, set_vars, conda_prompt_modifier):
        ps1 = self.environ.get('PS1', '')
        current_prompt_modifier = self.environ.get('CONDA_PROMPT_MODIFIER')
        if current_prompt_modifier:
            ps1 = re.sub(re.escape(current_prompt_modifier), r'', ps1)
        # Because we're using single-quotes to set shell variables, we need to handle the
        # proper escaping of single quotes that are already part of the string.
        # Best solution appears to be https://stackoverflow.com/a/1250279
        ps1 = ps1.replace("'", "'\"'\"'")
        set_vars.update({
            'PS1': conda_prompt_modifier + ps1,
        })

    def _hook_preamble(self):
        if on_win:
            return ('export CONDA_EXE="$(cygpath \'%s\')"\n'
                    'export CONDA_BAT="%s"'
                    % (context.conda_exe, join(context.conda_prefix, 'condacmd', 'conda.bat'))
                    )
        else:
            return 'export CONDA_EXE="%s"' % context.conda_exe


class CshActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = ':'.join
        self.sep = '/'
        self.path_conversion = native_path_to_unix
        self.script_extension = '.csh'
        self.tempfile_extension = None  # write instructions to stdout rather than a temp file
        self.command_join = ';\n'

        self.unset_var_tmpl = 'unsetenv %s'
        self.export_var_tmpl = 'setenv %s "%s"'
        self.set_var_tmpl = "set %s='%s'"
        self.run_script_tmpl = 'source "%s"'

        self.hook_source_path = join(CONDA_PACKAGE_ROOT, 'shell', 'etc', 'profile.d', 'conda.csh')

        super(CshActivator, self).__init__(arguments)

    def _update_prompt(self, set_vars, conda_prompt_modifier):
        prompt = self.environ.get('prompt', '')
        current_prompt_modifier = self.environ.get('CONDA_PROMPT_MODIFIER')
        if current_prompt_modifier:
            prompt = re.sub(re.escape(current_prompt_modifier), r'', prompt)
        set_vars.update({
            'prompt': conda_prompt_modifier + prompt,
        })

    def _hook_preamble(self):
        if on_win:
            return ('setenv CONDA_EXE `cygpath %s`\n'
                    'setenv _CONDA_ROOT `cygpath %s`\n'
                    'setenv _CONDA_EXE `cygpath %s`'
                    % (context.conda_exe, context.conda_prefix, context.conda_exe))
        else:
            return ('setenv CONDA_EXE "%s"\n'
                    'setenv _CONDA_ROOT "%s"\n'
                    'setenv _CONDA_EXE "%s"'
                    % (context.conda_exe, context.conda_prefix, context.conda_exe))


class XonshActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = ':'.join
        self.sep = '/'
        self.path_conversion = native_path_to_unix
        self.script_extension = '.xsh'
        self.tempfile_extension = '.xsh'
        self.command_join = '\n'

        self.unset_var_tmpl = 'del $%s'
        self.export_var_tmpl = "$%s = '%s'"
        self.set_var_tmpl = "$%s = '%s'"  # TODO: determine if different than export_var_tmpl
        self.run_script_tmpl = 'source "%s"'

        self.hook_source_path = join(CONDA_PACKAGE_ROOT, 'shell', 'conda.xsh')

        super(XonshActivator, self).__init__(arguments)

    def _hook_preamble(self):
        return 'CONDA_EXE = "%s"' % context.conda_exe


class CmdExeActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = ';'.join
        self.sep = '\\'
        self.path_conversion = path_identity
        self.script_extension = '.bat'
        self.tempfile_extension = '.bat'
        self.command_join = '\r\n' if on_win else '\n'

        self.unset_var_tmpl = '@SET %s='
        self.export_var_tmpl = '@SET "%s=%s"'
        self.set_var_tmpl = '@SET "%s=%s"'  # TODO: determine if different than export_var_tmpl
        self.run_script_tmpl = '@CALL "%s"'

        self.hook_source_path = None
        # TODO: cmd.exe doesn't get a hook function? Or do we need to do something different?
        #       Like, for cmd.exe only, put a special directory containing only conda.bat on PATH?

        super(CmdExeActivator, self).__init__(arguments)

    def _build_activate_shell_custom(self, export_vars):
        if on_win:
            import ctypes
            export_vars.update({
                "PYTHONIOENCODING": ctypes.cdll.kernel32.GetACP(),
            })

    def _hook_preamble(self):
        raise NotImplementedError()


class FishActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = '" "'.join
        self.sep = '/'
        self.path_conversion = native_path_to_unix
        self.script_extension = '.fish'
        self.tempfile_extension = None  # write instructions to stdout rather than a temp file
        self.command_join = ';\n'

        self.unset_var_tmpl = 'set -e %s'
        self.export_var_tmpl = 'set -gx %s "%s"'
        self.set_var_tmpl = 'set -g %s "%s"'
        self.run_script_tmpl = 'source "%s"'

        self.hook_source_path = join(CONDA_PACKAGE_ROOT, 'shell', 'etc', 'fish', 'conf.d',
                                     'conda.fish')

        super(FishActivator, self).__init__(arguments)

    def _hook_preamble(self):
        if on_win:
            return ('set -gx CONDA_EXE (cygpath "%s")\n'
                    'set _CONDA_ROOT (cygpath "%s")\n'
                    'set _CONDA_EXE (cygpath "%s")'
                    % (context.conda_exe, context.conda_prefix, context.conda_exe))
        else:
            return ('set -gx CONDA_EXE "%s"\n'
                    'set _CONDA_ROOT "%s"\n'
                    'set _CONDA_EXE "%s"'
                    % (context.conda_exe, context.conda_prefix, context.conda_exe))


class PowershellActivator(_Activator):

    def __init__(self, arguments=None):
        self.pathsep_join = ';'.join
        self.sep = '\\'
        self.path_conversion = path_identity
        self.script_extension = '.ps1'
        self.tempfile_extension = None  # write instructions to stdout rather than a temp file
        self.command_join = '\n'

        self.unset_var_tmpl = 'Remove-Variable %s'
        self.export_var_tmpl = '$env:%s = "%s"'
        self.set_var_tmpl = '$env:%s = "%s"'  # TODO: determine if different than export_var_tmpl
        self.run_script_tmpl = '. "%s"'

        self.hook_source_path = None  # TODO: doesn't yet exist

        super(PowershellActivator, self).__init__(arguments)

    def _hook_preamble(self):
        raise NotImplementedError()


activator_map = {
    'posix': PosixActivator,
    'ash': PosixActivator,
    'bash': PosixActivator,
    'dash': PosixActivator,
    'zsh': PosixActivator,
    'csh': CshActivator,
    'tcsh': CshActivator,
    'xonsh': XonshActivator,
    'cmd.exe': CmdExeActivator,
    'fish': FishActivator,
    'powershell': PowershellActivator,
}


def main(argv=None):
    from .common.compat import init_std_stream_encoding

    context.__init__()  # On import, context does not include SEARCH_PATH. This line fixes that.

    init_std_stream_encoding()
    argv = argv or sys.argv
    assert len(argv) >= 3
    assert argv[1].startswith('shell.')
    shell = argv[1].replace('shell.', '', 1)
    activator_args = argv[2:]
    try:
        activator_cls = activator_map[shell]
    except KeyError:
        raise CondaError("%s is not a supported shell." % shell)
    activator = activator_cls(activator_args)
    try:
        print(activator.execute(), end='')
        return 0
    except Exception as e:
        if isinstance(e, CondaError):
            print(text_type(e), file=sys.stderr)
            return e.return_code
        else:
            raise


if __name__ == '__main__':
    sys.exit(main())
