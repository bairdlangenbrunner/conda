# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
"""
Sections in this module are

  1. top-level functions
  2. plan creators
  3. plan runners
  4. individual operations
  5. helper functions

The top-level functions compose and execute full plans.

A plan is created by composing various individual operations.  The plan data structure is a
list of dicts, where each dict represents an individual operation.  The dict contains two
keys--`function` and `kwargs`--where function is the name of the individual operation function
within this module.

Each individual operation must

  a) return a `Result` (i.e. NEEDS_SUDO, MODIFIED, or NO_CHANGE)
  b) have no side effects if context.dry_run is True
  c) be verbose and descriptive about the changes being made or proposed is context.verbosity >= 1

The plan runner functions take the plan (list of dicts) as an argument, and then coordinate the
execution of each individual operation.  The docstring for `run_plan_elevated()` has details on
how that strategy is implemented.

"""
from __future__ import absolute_import, division, print_function, unicode_literals

from difflib import unified_diff
from errno import ENOENT
from glob import glob
from itertools import chain
import json
from logging import getLogger
import os
from os.path import abspath, basename, dirname, exists, expanduser, isdir, isfile, join
from random import randint
import re
import sys
from tempfile import NamedTemporaryFile

from .. import CONDA_PACKAGE_ROOT, CondaError, __version__ as CONDA_VERSION
from .._vendor.auxlib.ish import dals
from ..activate import CshActivator, FishActivator, PosixActivator, XonshActivator
from ..base.context import context
from ..common.compat import (PY2, ensure_binary, ensure_fs_path_encoding, ensure_unicode, on_mac,
                             on_win, open)
from ..common.path import (expand, get_bin_directory_short_path, get_python_short_path,
                           get_python_site_packages_short_path, win_path_ok)
from ..exceptions import CondaValueError
from ..gateways.disk.create import copy, mkdir_p
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.link import lexists
from ..gateways.disk.permissions import make_executable
from ..gateways.disk.read import compute_md5sum
from ..gateways.subprocess import subprocess_call

if on_win:
    if PY2:
        import _winreg as winreg
    else:
        import winreg
    from menuinst.knownfolders import get_folder_path, FOLDERID
    from menuinst.winshortcut import create_shortcut


log = getLogger(__name__)


class Result:
    NEEDS_SUDO = "needs sudo"
    MODIFIED = "modified"
    NO_CHANGE = "no change"


# #####################################################
# top-level functions
# #####################################################

def install(conda_prefix):
    plan = make_install_plan(conda_prefix)
    run_plan(plan)
    if not context.dry_run:
        assert not any(step['result'] == Result.NEEDS_SUDO for step in plan)
    print_plan_results(plan)


def initialize(conda_prefix, shells, for_user, for_system, anaconda_prompt):
    plan1 = []
    if os.getenv('CONDA_PIP_UNINITIALIZED') == 'true':
        plan1 = make_install_plan(conda_prefix)
        run_plan(plan1)
        if not context.dry_run:
            run_plan_elevated(plan1)

    plan2 = make_initialize_plan(conda_prefix, shells, for_user, for_system, anaconda_prompt)
    run_plan(plan2)
    if not context.dry_run:
        run_plan_elevated(plan2)

    plan = plan1 + plan2
    print_plan_results(plan)

    if any(step['result'] == Result.NEEDS_SUDO for step in plan):
        print("Operation failed.", file=sys.stderr)
        return 1


def initialize_dev(shell, dev_env_prefix=None, conda_source_root=None):
    # > alias conda-dev='eval "$(python -m conda init --dev)"'
    # > eval "$(python -m conda init --dev)"

    dev_env_prefix = expand(dev_env_prefix or sys.prefix)
    conda_source_root = expand(conda_source_root or os.getcwd())

    python_exe, python_version, site_packages_dir = _get_python_info(dev_env_prefix)

    if not isfile(join(conda_source_root, 'conda', '__main__.py')):
        raise CondaValueError("Directory is not a conda source root: %s" % conda_source_root)

    plan = make_install_plan(dev_env_prefix)
    plan.append({
        'function': remove_conda_in_sp_dir.__name__,
        'kwargs': {
            'target_path': site_packages_dir,
        },
    })
    plan.append({
        'function': make_conda_egg_link.__name__,
        'kwargs': {
            'target_path': join(site_packages_dir, 'conda.egg-link'),
            'conda_source_root': conda_source_root,
        },
    })
    plan.append({
        'function': modify_easy_install_pth.__name__,
        'kwargs': {
            'target_path': join(site_packages_dir, 'easy-install.pth'),
            'conda_source_root': conda_source_root,
        },
    })
    plan.append({
        'function': make_dev_egg_info_file.__name__,
        'kwargs': {
            'target_path': join(conda_source_root, 'conda.egg-info'),
        },
    })

    run_plan(plan)

    if context.dry_run or context.verbosity:
        print_plan_results(plan, sys.stderr)

    if any(step['result'] == Result.NEEDS_SUDO for step in plan):  # pragma: no cover
        raise CondaError("Operation failed. Privileged install disallowed for 'conda init --dev'.")

    env_vars = {
        'ADD_COV': '--cov-report xml --cov-report term-missing --cov conda',
        'PYTHONHASHSEED': str(randint(0, 4294967296)),
        'PYTHON_MAJOR_VERSION': python_version[0],
        'TEST_PLATFORM': 'win' if on_win else 'unix',
    }
    unset_env_vars = (
        'CONDA_DEFAULT_ENV',
        'CONDA_EXE',
        'CONDA_PREFIX',
        'CONDA_PREFIX_1',
        'CONDA_PREFIX_2',
        'CONDA_PROMPT_MODIFIER',
        'CONDA_PYTHON_EXE',
        'CONDA_SHLVL',
    )

    if shell == "bash":
        builder = []
        builder += ["unset %s" % unset_env_var for unset_env_var in unset_env_vars]
        builder += ["export %s='%s'" % (key, env_vars[key]) for key in sorted(env_vars)]
        sys_executable = abspath(sys.executable)
        if on_win:
            sys_executable = "$(cygpath '%s')" % sys_executable
        builder += [
            "eval \"$(\"%s\" -m conda shell.bash hook)\"" % sys_executable,
            "conda activate '%s'" % dev_env_prefix,
        ]
        print("\n".join(builder))
    elif shell == 'cmd.exe':
        builder = []
        builder += ["@IF NOT \"%CONDA_PROMPT_MODIFIER%\" == \"\" @CALL "
                    "SET \"PROMPT=%%PROMPT:%CONDA_PROMPT_MODIFIER%=%_empty_not_set_%%%\""]
        builder += ["@SET %s=" % unset_env_var for unset_env_var in unset_env_vars]
        builder += ['@SET "%s=%s"' % (key, env_vars[key]) for key in sorted(env_vars)]
        builder += [
            '@CALL \"%s\"' % join(dev_env_prefix, 'condacmd', 'conda_hook.bat'),
            '@IF %errorlevel% NEQ 0 @EXIT /B %errorlevel%',
            '@CALL \"%s\" activate \"%s\"' % (join(dev_env_prefix, 'condacmd', 'conda.bat'),
                                              dev_env_prefix),
            '@IF %errorlevel% NEQ 0 @EXIT /B %errorlevel%',
        ]
        if not context.dry_run:
            with open('dev-init.bat', 'w') as fh:
                fh.write('\n'.join(builder))
        if context.verbosity:
            print('\n'.join(builder))
        print("now run  > .\\dev-init.bat")
    else:
        raise NotImplementedError()


# #####################################################
# plan creators
# #####################################################

def make_install_plan(conda_prefix):
    try:
        python_exe, python_version, site_packages_dir = _get_python_info(conda_prefix)
    except EnvironmentError:
        python_exe, python_version, site_packages_dir = None, None, None  # NOQA

    plan = []

    # ######################################
    # executables
    # ######################################
    if on_win:
        conda_exe_path = join(conda_prefix, 'Scripts', 'conda-script.py')
        conda_env_exe_path = join(conda_prefix, 'Scripts', 'conda-env-script.py')
        plan.append({
            'function': make_entry_point_exe.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'Scripts', 'conda.exe'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': make_entry_point_exe.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'Scripts', 'conda-env.exe'),
                'conda_prefix': conda_prefix,
            },
        })
    else:
        conda_exe_path = join(conda_prefix, 'bin', 'conda')
        conda_env_exe_path = join(conda_prefix, 'bin', 'conda-env')

    plan.append({
        'function': make_entry_point.__name__,
        'kwargs': {
            'target_path': conda_exe_path,
            'conda_prefix': conda_prefix,
            'module': 'conda.cli',
            'func': 'main',
        },
    })
    plan.append({
        'function': make_entry_point.__name__,
        'kwargs': {
            'target_path': conda_env_exe_path,
            'conda_prefix': conda_prefix,
            'module': 'conda_env.cli.main',
            'func': 'main',
        },
    })

    # ######################################
    # shell wrappers
    # ######################################
    if on_win:
        plan.append({
            'function': install_condacmd_conda_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', 'conda.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_condacmd_conda_activate_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', '_conda_activate.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_condacmd_conda_auto_activate_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', 'conda_auto_activate.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_condacmd_hook_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', 'conda_hook.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_Scripts_activate_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'Scripts', 'activate.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_activate_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', 'activate.bat'),
                'conda_prefix': conda_prefix,
            },
        })
        plan.append({
            'function': install_deactivate_bat.__name__,
            'kwargs': {
                'target_path': join(conda_prefix, 'condacmd', 'deactivate.bat'),
                'conda_prefix': conda_prefix,
            },
        })

    plan.append({
        'function': install_activate.__name__,
        'kwargs': {
            'target_path': join(conda_prefix, get_bin_directory_short_path(), 'activate'),
            'conda_prefix': conda_prefix,
        },
    })
    plan.append({
        'function': install_deactivate.__name__,
        'kwargs': {
            'target_path': join(conda_prefix, get_bin_directory_short_path(), 'deactivate'),
            'conda_prefix': conda_prefix,
        },
    })

    plan.append({
        'function': install_conda_sh.__name__,
        'kwargs': {
            'target_path': join(conda_prefix, 'etc', 'profile.d', 'conda.sh'),
            'conda_prefix': conda_prefix,
        },
    })
    plan.append({
        'function': install_conda_fish.__name__,
        'kwargs': {
            'target_path': join(conda_prefix, 'etc', 'fish', 'conf.d', 'conda.fish'),
            'conda_prefix': conda_prefix,
        },
    })
    if site_packages_dir:
        plan.append({
            'function': install_conda_xsh.__name__,
            'kwargs': {
                'target_path': join(site_packages_dir, 'xonsh', 'conda.xsh'),
                'conda_prefix': conda_prefix,
            },
        })
    else:
        print("WARNING: Cannot install xonsh wrapper without a python interpreter in prefix: "
              "%s" % conda_prefix, file=sys.stderr)
    plan.append({
        'function': install_conda_csh.__name__,
        'kwargs': {
            'target_path': join(conda_prefix, 'etc', 'profile.d', 'conda.csh'),
            'conda_prefix': conda_prefix,
        },
    })
    return plan


def make_initialize_plan(conda_prefix, shells, for_user, for_system, anaconda_prompt):
    plan = make_install_plan(conda_prefix)
    shells = set(shells)
    if shells & {'bash', 'zsh'}:
        if 'bash' in shells and for_user:
            bashrc_path = expand(join('~', '.bash_profile' if on_mac else '.bashrc'))
            plan.append({
                'function': init_sh_user.__name__,
                'kwargs': {
                    'target_path': bashrc_path,
                    'conda_prefix': conda_prefix,
                    'shell': 'bash',
                },
            })

        if 'zsh' in shells and for_user:
            zshrc_path = expand(join('~', '.zshrc'))
            plan.append({
                'function': init_sh_user.__name__,
                'kwargs': {
                    'target_path': zshrc_path,
                    'conda_prefix': conda_prefix,
                    'shell': 'zsh',
                },
            })

        if for_system:
            plan.append({
                'function': init_sh_system.__name__,
                'kwargs': {
                    'target_path': '/etc/profile.d/conda.sh',
                    'conda_prefix': conda_prefix,
                },
            })

    if 'fish' in shells:
        if for_user:
            config_fish_path = expand(join('~', '.config', 'config.fish'))
            plan.append({
                'function': init_fish_user.__name__,
                'kwargs': {
                    'target_path': config_fish_path,
                    'conda_prefix': conda_prefix,
                },
            })
        if for_system:
            raise NotImplementedError()

    if 'tcsh' in shells:
        if for_user:
            raise NotImplementedError()
        if for_system:
            raise NotImplementedError()

    if 'powershell' in shells:
        if for_user:
            raise NotImplementedError()
        if for_system:
            raise NotImplementedError()

    if 'cmd.exe' in shells:
        if for_user:
            plan.append({
                'function': init_cmd_exe_registry.__name__,
                'kwargs': {
                    'target_path': 'HKEY_CURRENT_USER\\Software\\Microsoft\\'
                                   'Command Processor\\AutoRun',
                    'conda_prefix': conda_prefix,
                },
            })
        if for_system:
            plan.append({
                'function': init_cmd_exe_registry.__name__,
                'kwargs': {
                    'target_path': 'HKEY_LOCAL_MACHINE\\Software\\Microsoft\\'
                                   'Command Processor\\AutoRun',
                    'conda_prefix': conda_prefix,
                },
            })
        if anaconda_prompt:
            plan.append({
                'function': install_anaconda_prompt.__name__,
                'kwargs': {
                    'target_path': join(conda_prefix, 'condacmd', 'Anaconda Prompt.lnk'),
                    'conda_prefix': conda_prefix,
                },
            })
            if on_win:
                desktop_dir, exception = get_folder_path(FOLDERID.Desktop)
                assert not exception
            else:
                desktop_dir = join(expanduser('~'), "Desktop")
            plan.append({
                'function': install_anaconda_prompt.__name__,
                'kwargs': {
                    'target_path': join(desktop_dir, "Anaconda Prompt.lnk"),
                    'conda_prefix': conda_prefix,
                },
            })

    return plan


# #####################################################
# plan runners
# #####################################################

def run_plan(plan):
    for step in plan:
        previous_result = step.get('result', None)
        if previous_result in (Result.MODIFIED, Result.NO_CHANGE):
            continue
        try:
            result = globals()[step['function']](*step.get('args', ()), **step.get('kwargs', {}))
        except EnvironmentError as e:
            log.info("%s: %r", step['function'], e, exc_info=True)
            result = Result.NEEDS_SUDO
        step['result'] = result


def run_plan_elevated(plan):
    """
    The strategy of this function differs between unix and Windows.  Both strategies use a
    subprocess call, where the subprocess is run with elevated privileges.  The executable
    invoked with the subprocess is `python -m conda.core.initialize`, so see the
    `if __name__ == "__main__"` at the bottom of this module.

    For unix platforms, we convert the plan list to json, and then call this module with
    `sudo python -m conda.core.initialize` while piping the plan json to stdin.  We collect json
    from stdout for the results of the plan execution with elevated privileges.

    For Windows, we create a temporary file that holds the json content of the plan.  The
    subprocess reads the content of the file, modifies the content of the file with updated
    execution status, and then closes the file.  This process then reads the content of that file
    for the individual operation execution results, and then deletes the file.

    """

    if any(step['result'] == Result.NEEDS_SUDO for step in plan):
        if on_win:
            from menuinst.win_elevate import runAsAdmin
            # https://github.com/ContinuumIO/menuinst/blob/master/menuinst/windows/win_elevate.py  # no stdin / stdout / stderr pipe support  # NOQA
            # https://github.com/saltstack/salt-windows-install/blob/master/deps/salt/python/App/Lib/site-packages/win32/Demos/pipes/runproc.py  # NOQA
            # https://github.com/twonds/twisted/blob/master/twisted/internet/_dumbwin32proc.py
            # https://stackoverflow.com/a/19982092/2127762
            # https://www.codeproject.com/Articles/19165/Vista-UAC-The-Definitive-Guide

            # from menuinst.win_elevate import isUserAdmin, runAsAdmin
            # I do think we can pipe to stdin, so we're going to have to write to a temp file and read in the elevated process  # NOQA

            temp_path = None
            try:
                with NamedTemporaryFile('w+b', suffix='.json', delete=False) as tf:
                    # the default mode is 'w+b', and universal new lines don't work in that mode
                    tf.write(ensure_binary(json.dumps(plan, ensure_ascii=False)))
                    temp_path = tf.name
                rc = runAsAdmin((sys.executable, '-m',  'conda.initialize',  '"%s"' % temp_path))
                assert rc == 0

                with open(temp_path) as fh:
                    _plan = json.loads(ensure_unicode(fh.read()))
            finally:
                if temp_path and lexists(temp_path):
                    rm_rf(temp_path)

        else:
            stdin = json.dumps(plan)
            result = subprocess_call(
                'sudo %s -m conda.initialize' % sys.executable,
                env={},
                path=os.getcwd(),
                stdin=stdin
            )
            stderr = result.stderr.strip()
            if stderr:
                print(stderr, file=sys.stderr)
            _plan = json.loads(result.stdout.strip())

        del plan[:]
        plan.extend(_plan)


def run_plan_from_stdin():
    stdin = sys.stdin.read().strip()
    plan = json.loads(stdin)
    run_plan(plan)
    sys.stdout.write(json.dumps(plan))


def run_plan_from_temp_file(temp_path):
    with open(temp_path) as fh:
        plan = json.loads(ensure_unicode(fh.read()))
    run_plan(plan)
    with open(temp_path, 'w+b') as fh:
        fh.write(ensure_binary(json.dumps(plan, ensure_ascii=False)))


def print_plan_results(plan, stream=None):
    if not stream:
        stream = sys.stdout
    for step in plan:
        print("%-14s%s" % (step.get('result'), step['kwargs']['target_path']), file=stream)

    changed = any(step.get('result') == Result.MODIFIED for step in plan)
    if changed:
        print("\n==> For changes to take effect, close and re-open your current shell. <==\n",
              file=stream)
    else:
        print("No action taken.", file=stream)


# #####################################################
# individual operations
# #####################################################

def make_entry_point(target_path, conda_prefix, module, func):
    # target_path: join(conda_prefix, 'bin', 'conda')
    conda_ep_path = target_path

    if isfile(conda_ep_path):
        with open(conda_ep_path) as fh:
            original_ep_content = fh.read()
    else:
        original_ep_content = ""

    if on_win:
        # no shebang needed on windows
        new_ep_content = ""
    else:
        new_ep_content = "#!%s\n" % join(conda_prefix, get_python_short_path())

    conda_extra = dals("""
    # Before any more imports, leave cwd out of sys.path for internal 'conda shell.*' commands.
    # see https://github.com/conda/conda/issues/6549
    if len(sys.argv) > 1 and sys.argv[1].startswith('shell.') and sys.path and sys.path[0] == '':
        # The standard first entry in sys.path is an empty string,
        # and os.path.abspath('') expands to os.getcwd().
        del sys.path[0]
    """)

    new_ep_content += dals("""
    # -*- coding: utf-8 -*-
    import sys
    %(extra)s
    if __name__ == '__main__':
        from %(module)s import %(func)s
        sys.exit(%(func)s())
    """) % {
        'extra': conda_extra if module == 'conda.cli' else '',
        'module': module,
        'func': func,
    }

    if new_ep_content != original_ep_content:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(original_ep_content, new_ep_content))
        if not context.dry_run:
            mkdir_p(dirname(conda_ep_path))
            with open(conda_ep_path, 'w') as fdst:
                fdst.write(new_ep_content)
            if not on_win:
                make_executable(conda_ep_path)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def make_entry_point_exe(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'Scripts', 'conda.exe')
    exe_path = target_path
    bits = 8 * tuple.__itemsize__
    source_exe_path = join(CONDA_PACKAGE_ROOT, 'shell', 'cli-%d.exe' % bits)
    if isfile(exe_path):
        if compute_md5sum(exe_path) == compute_md5sum(source_exe_path):
            return Result.NO_CHANGE

    if not context.dry_run:
        if not isdir(dirname(exe_path)):
            mkdir_p(dirname(exe_path))
        # prefer copy() over create_hard_link_or_copy() because of windows file deletion issues
        # with open processes
        copy(source_exe_path, exe_path)
    return Result.MODIFIED


def install_anaconda_prompt(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'Anaconda Prompt.lnk')
    # target: join(os.environ["HOMEPATH"], "Desktop", "Anaconda Prompt.lnk")
    icon_path = join(CONDA_PACKAGE_ROOT, 'shell', 'conda_icon.ico')

    args = (
        '/K',
        '""%s" && "%s""' % (join(conda_prefix, 'condacmd', 'conda_hook.bat'),
                            join(conda_prefix, 'condacmd', 'conda_auto_activate.bat')),
    )
    # The API for the call to 'create_shortcut' has 3
    # required arguments (path, description, filename)
    # and 4 optional ones (args, working_dir, icon_path, icon_index).
    if not context.dry_run:
        create_shortcut(
            "%windir%\\System32\\cmd.exe",
            "Anconda Prompt",
            '' + target_path,
            ' '.join(args),
            '' + expanduser('~'),
            '' + icon_path,
        )
    # TODO: need to make idempotent / support NO_CHANGE
    return Result.MODIFIED


def _install_file(target_path, file_content):
    if isfile(target_path):
        with open(target_path) as fh:
            original_content = fh.read()
    else:
        original_content = ""

    new_content = file_content

    if new_content != original_content:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(original_content, new_content))
        if not context.dry_run:
            mkdir_p(dirname(target_path))
            with open(target_path, 'w') as fdst:
                fdst.write(new_content)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def install_conda_sh(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'etc', 'profile.d', 'conda.sh')
    file_content = PosixActivator().hook(auto_activate_base=False)
    return _install_file(target_path, file_content)


def install_Scripts_activate_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'Scripts', 'activate.bat')
    src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'Scripts', 'activate.bat')
    with open(src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_activate_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'activate.bat')
    src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', 'activate.bat')
    with open(src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_deactivate_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'deactivate.bat')
    src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', 'deactivate.bat')
    with open(src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_activate(target_path, conda_prefix):
    # target_path: join(conda_prefix, get_bin_directory_short_path(), 'activate')
    src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'bin', 'activate')
    file_content = (
        "#!/bin/sh\n"
        "_CONDA_ROOT=\"%s\"\n"
    ) % conda_prefix
    with open(src_path) as fsrc:
        file_content += fsrc.read()
    return _install_file(target_path, file_content)


def install_deactivate(target_path, conda_prefix):
    # target_path: join(conda_prefix, get_bin_directory_short_path(), 'deactivate')
    src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'bin', 'deactivate')
    file_content = (
        "#!/bin/sh\n"
        "_CONDA_ROOT=\"%s\"\n"
    ) % conda_prefix
    with open(src_path) as fsrc:
        file_content += fsrc.read()
    return _install_file(target_path, file_content)


def install_condacmd_conda_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'conda.bat')
    conda_bat_src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', 'conda.bat')
    with open(conda_bat_src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_condacmd_conda_activate_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', '_conda_activate.bat')
    conda_bat_src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', '_conda_activate.bat')
    with open(conda_bat_src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_condacmd_conda_auto_activate_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'conda_auto_activate.bat')
    conda_bat_src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', 'conda_auto_activate.bat')
    with open(conda_bat_src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_condacmd_hook_bat(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'condacmd', 'conda_hook.bat')
    conda_bat_src_path = join(CONDA_PACKAGE_ROOT, 'shell', 'condacmd', 'conda_hook.bat')
    with open(conda_bat_src_path) as fsrc:
        file_content = fsrc.read()
    return _install_file(target_path, file_content)


def install_conda_fish(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'etc', 'fish', 'conf.d', 'conda.fish')
    file_content = FishActivator().hook(auto_activate_base=False)
    return _install_file(target_path, file_content)


def install_conda_xsh(target_path, conda_prefix):
    # target_path: join(site_packages_dir, 'xonsh', 'conda.xsh')
    file_content = XonshActivator().hook(auto_activate_base=False)
    return _install_file(target_path, file_content)


def install_conda_csh(target_path, conda_prefix):
    # target_path: join(conda_prefix, 'etc', 'profile.d', 'conda.csh')
    file_content = CshActivator().hook(auto_activate_base=False)
    return _install_file(target_path, file_content)


def _config_fish_content(conda_prefix):
    if on_win:
        from ..activate import native_path_to_unix
        conda_exe = native_path_to_unix(join(conda_prefix, 'Scripts', 'conda.exe'))
    else:
        conda_exe = join(conda_prefix, 'bin', 'conda')
    conda_initialize_content = dals("""
    # >>> conda initialize >>>
    # !! Contents within this block are managed by 'conda init' !!
    eval (eval %(conda_exe)s shell.fish hook $argv)
    # <<< conda initialize <<<
    """) % {
        'conda_exe': conda_exe,
    }
    return conda_initialize_content


def init_fish_user(target_path, conda_prefix):
    # target_path: ~/.config/config.fish
    user_rc_path = target_path

    with open(user_rc_path) as fh:
        rc_content = fh.read()

    rc_original_content = rc_content

    conda_initialize_content = _config_fish_content(conda_prefix)

    if not on_win:
        rc_content = re.sub(
            r"^[ \t]*?(set -gx PATH ([\'\"]?).*?%s\/bin\2 [^\n]*?\$PATH)"
            r"" % basename(conda_prefix),
            r"# \1  # commented out by conda initialize",
            rc_content,
            flags=re.MULTILINE,
        )

    rc_content = re.sub(
        r"^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*etc\/fish\/conf\.d\/conda\.fish.*?)\n"
        r"(conda activate.*?)$",
        r"# \1  # commented out by conda initialize\n# \2  # commented out by conda initialize",
        rc_content,
        flags=re.MULTILINE,
    )
    rc_content = re.sub(
        r"^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*etc\/fish\/conda\.d\/conda\.fish.*?)$",
        r"# \1  # commented out by conda initialize",
        rc_content,
        flags=re.MULTILINE,
    )

    replace_str = "__CONDA_REPLACE_ME_123__"
    rc_content = re.sub(
        r"^# >>> conda initialize >>>$([\s\S]*?)# <<< conda initialize <<<\n$",
        replace_str,
        rc_content,
        flags=re.MULTILINE,
    )
    # TODO: maybe remove all but last of replace_str, if there's more than one occurrence
    rc_content = rc_content.replace(replace_str, conda_initialize_content)

    if "# >>> conda initialize >>>" not in rc_content:
        rc_content += '\n%s\n' % conda_initialize_content

    if rc_content != rc_original_content:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(rc_original_content, rc_content))
        if not context.dry_run:
            with open(user_rc_path, 'w') as fh:
                fh.write(rc_content)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def _bashrc_content(conda_prefix, shell):
    if on_win:
        from ..activate import native_path_to_unix
        conda_exe = native_path_to_unix(join(conda_prefix, 'Scripts', 'conda.exe'))
        conda_initialize_content = dals("""
        # >>> conda initialize >>>
        # !! Contents within this block are managed by 'conda init' !!
        eval "$('%(conda_exe)s' shell.%(shell)s hook)"
        # <<< conda initialize <<<
        """) % {
            'conda_exe': conda_exe,
            'shell': shell,
        }
    else:
        conda_exe = join(conda_prefix, 'bin', 'conda')
        conda_initialize_content = dals("""
        # >>> conda initialize >>>
        # !! Contents within this block are managed by 'conda init' !!
        __conda_setup="$('%(conda_exe)s' shell.%(shell)s hook 2> /dev/null)"
        if [ $? -eq 0 ]; then
            eval "$__conda_setup"
        else
            if [ -f "%(conda_prefix)s/etc/profile.d/conda.sh" ]; then
                . "%(conda_prefix)s/etc/profile.d/conda.sh"
            else
                export PATH="%(conda_bin)s:$PATH"
            fi
        fi
        unset __conda_setup
        # <<< conda initialize <<<
        """) % {
            'conda_exe': conda_exe,
            'shell': shell,
            'conda_bin': dirname(conda_exe),
            'conda_prefix': conda_prefix,
        }
    return conda_initialize_content


def init_sh_user(target_path, conda_prefix, shell):
    # target_path: ~/.bash_profile
    user_rc_path = target_path

    with open(user_rc_path) as fh:
        rc_content = fh.read()

    rc_original_content = rc_content

    conda_initialize_content = _bashrc_content(conda_prefix, shell)

    if not on_win:
        rc_content = re.sub(
            r"^[ \t]*?(export PATH=[\'\"].*?%s\/bin:\$PATH[\'\"])"
            r"" % basename(conda_prefix),
            r"# \1  # commented out by conda initialize",
            rc_content,
            flags=re.MULTILINE,
        )

    rc_content = re.sub(
        r"^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*etc\/profile\.d\/conda\.sh.*?)\n"
        r"(conda activate.*?)$",
        r"# \1  # commented out by conda initialize\n# \2  # commented out by conda initialize",
        rc_content,
        flags=re.MULTILINE,
    )
    rc_content = re.sub(
        r"^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*etc\/profile\.d\/conda\.sh.*?)$",
        r"# \1  # commented out by conda initialize",
        rc_content,
        flags=re.MULTILINE,
    )

    if on_win:
        rc_content = re.sub(
            r"^[ \t]*^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*Scripts[/\\]activate.*?)$",
            r"# \1  # commented out by conda initialize",
            rc_content,
            flags=re.MULTILINE,
        )
    else:
        rc_content = re.sub(
            r"^[ \t]*^[ \t]*[^#\n]?[ \t]*((?:source|\.) .*bin/activate.*?)$",
            r"# \1  # commented out by conda initialize",
            rc_content,
            flags=re.MULTILINE,
        )

    replace_str = "__CONDA_REPLACE_ME_123__"
    rc_content = re.sub(
        r"^# >>> conda initialize >>>$([\s\S]*?)# <<< conda initialize <<<\n$",
        replace_str,
        rc_content,
        flags=re.MULTILINE,
    )
    # TODO: maybe remove all but last of replace_str, if there's more than one occurrence
    rc_content = rc_content.replace(replace_str, conda_initialize_content)

    if "# >>> conda initialize >>>" not in rc_content:
        rc_content += '\n%s\n' % conda_initialize_content

    if rc_content != rc_original_content:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(rc_original_content, rc_content))
        if not context.dry_run:
            with open(user_rc_path, 'w') as fh:
                fh.write(rc_content)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def init_sh_system(target_path, conda_prefix):
    # target_path: '/etc/profile.d/conda.sh'
    conda_sh_system_path = target_path

    if exists(conda_sh_system_path):
        with open(conda_sh_system_path) as fh:
            conda_sh_system_contents = fh.read()
    else:
        conda_sh_system_contents = ""
    conda_sh_contents = _bashrc_content(conda_prefix, 'posix')
    if conda_sh_system_contents != conda_sh_contents:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(conda_sh_contents, conda_sh_system_contents))
        if not context.dry_run:
            if lexists(conda_sh_system_path):
                rm_rf(conda_sh_system_path)
            mkdir_p(dirname(conda_sh_system_path))
            with open(conda_sh_system_path, 'w') as fh:
                fh.write(conda_sh_contents)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def _read_windows_registry(target_path):  # pragma: no cover
    # HKEY_LOCAL_MACHINE\Software\Microsoft\Command Processor\AutoRun
    # HKEY_CURRENT_USER\Software\Microsoft\Command Processor\AutoRun
    # returns value_value, value_type  -or-  None, None if target does not exist
    main_key, the_rest = target_path.split('\\', 1)
    subkey_str, value_name = the_rest.rsplit('\\', 1)
    main_key = getattr(winreg, main_key)
    try:
        key = winreg.OpenKey(main_key, subkey_str, 0, winreg.KEY_READ)
    except EnvironmentError as e:
        if e.errno != ENOENT:
            raise
        return None, None
    try:
        value_tuple = winreg.QueryValueEx(key, value_name)
        value_value = value_tuple[0].strip()
        value_type = value_tuple[1]
        return value_value, value_type
    finally:
        winreg.CloseKey(key)


def _write_windows_registry(target_path, value_value, value_type):  # pragma: no cover
    main_key, the_rest = target_path.split('\\', 1)
    subkey_str, value_name = the_rest.rsplit('\\', 1)
    main_key = getattr(winreg, main_key)
    try:
        key = winreg.OpenKey(main_key, subkey_str, 0, winreg.KEY_WRITE)
    except EnvironmentError as e:
        if e.errno != ENOENT:
            raise
        key = winreg.CreateKey(main_key, subkey_str)
    try:
        winreg.SetValueEx(key, value_name, 0, value_type, value_value)
    finally:
        winreg.CloseKey(key)


def init_cmd_exe_registry(target_path, conda_prefix):
    # HKEY_LOCAL_MACHINE\Software\Microsoft\Command Processor\AutoRun
    # HKEY_CURRENT_USER\Software\Microsoft\Command Processor\AutoRun

    prev_value, value_type = _read_windows_registry(target_path)
    if prev_value is None:
        prev_value = ""
        value_type = winreg.REG_EXPAND_SZ

    hook_path = '"%s"' % join(conda_prefix, 'condacmd', 'conda_hook.bat')
    replace_str = "__CONDA_REPLACE_ME_123__"
    new_value = re.sub(
        r'(\".*?conda[-_]hook\.bat\")',
        replace_str,
        prev_value,
        count=1,
        flags=re.IGNORECASE | re.UNICODE,
    )
    new_value = new_value.replace(replace_str, hook_path)
    if hook_path not in new_value:
        if new_value:
            new_value += ' & ' + hook_path
        else:
            new_value = hook_path

    if prev_value != new_value:
        if context.verbosity:
            print('\n')
            print(target_path)
            print(make_diff(prev_value, new_value))
        if not context.dry_run:
            _write_windows_registry(target_path, new_value, value_type)
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def remove_conda_in_sp_dir(target_path):
    # target_path: site_packages_dir
    modified = False
    site_packages_dir = target_path
    rm_rf_these = chain.from_iterable((
        glob(join(site_packages_dir, "conda-*info")),
        glob(join(site_packages_dir, "conda.*")),
        glob(join(site_packages_dir, "conda-*.egg")),
    ))
    rm_rf_these = (p for p in rm_rf_these if not p.endswith('conda.egg-link'))
    for fn in rm_rf_these:
        print("rm -rf %s" % join(site_packages_dir, fn), file=sys.stderr)
        if not context.dry_run:
            rm_rf(join(site_packages_dir, fn))
        modified = True
    others = (
        "conda",
        "conda_env",
    )
    for other in others:
        path = join(site_packages_dir, other)
        if lexists(path):
            print("rm -rf %s" % path, file=sys.stderr)
            if not context.dry_run:
                rm_rf(path)
            modified = True
    if modified:
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def make_conda_egg_link(target_path, conda_source_root):
    # target_path: join(site_packages_dir, 'conda.egg-link')
    conda_egg_link_contents = conda_source_root + os.linesep

    if isfile(target_path):
        with open(target_path) as fh:
            conda_egg_link_contents_old = fh.read()
    else:
        conda_egg_link_contents_old = ""

    if conda_egg_link_contents_old != conda_egg_link_contents:
        if context.verbosity:
            print('\n', file=sys.stderr)
            print(target_path, file=sys.stderr)
            print(make_diff(conda_egg_link_contents_old, conda_egg_link_contents), file=sys.stderr)
        if not context.dry_run:
            with open(target_path, 'w') as fh:
                fh.write(ensure_fs_path_encoding(conda_egg_link_contents))
        return Result.MODIFIED
    else:
        return Result.NO_CHANGE


def modify_easy_install_pth(target_path, conda_source_root):
    # target_path: join(site_packages_dir, 'easy-install.pth')
    easy_install_new_line = conda_source_root

    if isfile(target_path):
        with open(target_path) as fh:
            old_contents = fh.read()
    else:
        old_contents = ""

    old_contents_lines = old_contents.splitlines()
    if easy_install_new_line in old_contents_lines:
        return Result.NO_CHANGE

    ln_end = os.sep + "conda"
    old_contents_lines = tuple(ln for ln in old_contents_lines if not ln.endswith(ln_end))
    new_contents = easy_install_new_line + '\n' + '\n'.join(old_contents_lines) + '\n'

    if context.verbosity:
        print('\n', file=sys.stderr)
        print(target_path, file=sys.stderr)
        print(make_diff(old_contents, new_contents), file=sys.stderr)
    if not context.dry_run:
        with open(target_path, 'w') as fh:
            fh.write(ensure_fs_path_encoding(new_contents))
    return Result.MODIFIED


def make_dev_egg_info_file(target_path):
    # target_path: join(conda_source_root, 'conda.egg-info')

    if isfile(target_path):
        with open(target_path) as fh:
            old_contents = fh.read()
    else:
        old_contents = ""

    new_contents = dals("""
    Metadata-Version: 1.1
    Name: conda
    Version: %s
    Platform: UNKNOWN
    Summary: OS-agnostic, system-level binary package manager.
    """) % CONDA_VERSION

    if old_contents == new_contents:
        return Result.NO_CHANGE

    if context.verbosity:
        print('\n', file=sys.stderr)
        print(target_path, file=sys.stderr)
        print(make_diff(old_contents, new_contents), file=sys.stderr)
    if not context.dry_run:
        if lexists(target_path):
            rm_rf(target_path)
        with open(target_path, 'w') as fh:
            fh.write(new_contents)
    return Result.MODIFIED


# #####################################################
# helper functions
# #####################################################

def make_diff(old, new):
    return '\n'.join(unified_diff(old.splitlines(), new.splitlines()))


def _get_python_info(prefix):
    python_exe = join(prefix, get_python_short_path())
    result = subprocess_call("%s --version" % python_exe)
    stdout, stderr = result.stdout.strip(), result.stderr.strip()
    if stderr:
        python_version = stderr.split()[1]
    elif stdout:  # pragma: no cover
        python_version = stdout.split()[1]
    else:  # pragma: no cover
        raise ValueError("No python version information available.")

    site_packages_dir = join(prefix,
                             win_path_ok(get_python_site_packages_short_path(python_version)))
    return python_exe, python_version, site_packages_dir


if __name__ == "__main__":
    if on_win:
        temp_path = sys.argv[1]
        run_plan_from_temp_file(temp_path)
    else:
        run_plan_from_stdin()
