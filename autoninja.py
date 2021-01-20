#!/usr/bin/env vpython
# Copyright (c) 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""
This script (intended to be invoked by autoninja or autoninja.bat) detects
whether a build is accelerated using a service like goma. If so, it runs with a
large -j value, and otherwise it chooses a small one. This auto-adjustment
makes using remote build acceleration simpler and safer, and avoids errors that
can cause slow goma builds or swap-storms on unaccelerated builds.
"""

from __future__ import print_function

import multiprocessing
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# The -t tools are incompatible with -j
t_specified = False
j_specified = False
offline = False
output_dir = '.'
input_args = sys.argv
# On Windows the autoninja.bat script passes along the arguments enclosed in
# double quotes. This prevents multiple levels of parsing of the special '^'
# characters needed when compiling a single file but means that this script gets
# called with a single argument containing all of the actual arguments,
# separated by spaces. When this case is detected we need to do argument
# splitting ourselves. This means that arguments containing actual spaces are
# not supported by autoninja, but that is not a real limitation.
if (sys.platform.startswith('win') and len(sys.argv) == 2 and
    input_args[1].count(' ') > 0):
  input_args = sys.argv[:1] + sys.argv[1].split()

# Ninja uses getopt_long, which allow to intermix non-option arguments.
# To leave non supported parameters untouched, we do not use getopt.
for index, arg in enumerate(input_args[1:]):
  if arg.startswith('-j'):
    j_specified = True
  if arg.startswith('-t'):
    t_specified = True
  if arg == '-C':
    # + 1 to get the next argument and +1 because we trimmed off input_args[0]
    output_dir = input_args[index + 2]
  elif arg.startswith('-C'):
    # Support -Cout/Default
    output_dir = arg[2:]
  elif arg == '-o' or arg == '--offline':
    offline = True
  elif arg == '-h':
    print('autoninja: Use -o/--offline to temporary disable goma.',
          file=sys.stderr)
    print(file=sys.stderr)

# Strip -o/--offline so ninja doesn't see them.
input_args = [ arg for arg in input_args if arg != '-o' and arg != '--offline']

use_goma = False
use_rbe = False

# Currently get reclient binary and config dirs relative to output_dir.  If
# they exist and using rbe, then automatically call bootstrap to start
# reproxy.  This works under the current assumption that the output
# directory is two levels up from chromium/src.
reclient_bin_dir = os.path.join(output_dir, "../../buildtools/reclient")
reclient_cfg = os.path.join(
  output_dir, "../../buildtools/reclient_cfgs/reproxy.cfg")

# Attempt to auto-detect remote build acceleration. We support gn-based
# builds, where we look for args.gn in the build tree, and cmake-based builds
# where we look for rules.ninja.
if os.path.exists(os.path.join(output_dir, 'args.gn')):
  with open(os.path.join(output_dir, 'args.gn')) as file_handle:
    for line in file_handle:
      # Either use_goma or use_rbe activate build acceleration.
      #
      # This test can match multi-argument lines. Examples of this are:
      # is_debug=false use_goma=true is_official_build=false
      # use_goma=false# use_goma=true This comment is ignored
      #
      # Anything after a comment is not consider a valid argument.
      line_without_comment = line.split('#')[0]
      if re.search(r'(^|\s)(use_goma)\s*=\s*true($|\s)',
                   line_without_comment):
        use_goma = True
        continue
      if re.search(r'(^|\s)(use_rbe)\s*=\s*true($|\s)',
                   line_without_comment):
        use_rbe = True
        continue
else:
  for relative_path in [
      '',  # GN keeps them in the root of output_dir
      'CMakeFiles'
  ]:
    path = os.path.join(output_dir, relative_path, 'rules.ninja')
    if os.path.exists(path):
      with open(path) as file_handle:
        for line in file_handle:
          if re.match(r'^\s*command\s*=\s*\S+gomacc', line):
            use_goma = True
            break

# If GOMA_DISABLED is set to "true", "t", "yes", "y", or "1" (case-insensitive)
# then gomacc will use the local compiler instead of doing a goma compile. This
# is convenient if you want to briefly disable goma. It avoids having to rebuild
# the world when transitioning between goma/non-goma builds. However, it is not
# as fast as doing a "normal" non-goma build because an extra process is created
# for each compile step. Checking this environment variable ensures that
# autoninja uses an appropriate -j value in this situation.
goma_disabled_env = os.environ.get('GOMA_DISABLED', '0').lower()
if offline or goma_disabled_env in ['true', 't', 'yes', 'y', '1']:
  use_goma = False

if use_goma:
  gomacc_file = 'gomacc.exe' if sys.platform.startswith('win') else 'gomacc'
  gomacc_path = os.path.join(SCRIPT_DIR, '.cipd_bin', gomacc_file)
  # Don't invoke gomacc if it doesn't exist.
  if os.path.exists(gomacc_path):
    # Check to make sure that goma is running. If not, don't start the build.
    status = subprocess.run([gomacc_path, 'port'],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    if status == 1:
      print('echo Goma is not running. Use "goma_ctl start" to start it.',
            file=sys.stderr)
      if sys.platform.startswith('win'):
        print('exit /b 1')
      else:
        print('exit 1')
      sys.exit(1)

# Specify ninja.exe on Windows so that ninja.bat can call autoninja and not
# be called back.
ninja_exe = 'ninja.exe' if sys.platform.startswith('win') else 'ninja'
ninja_exe_path = os.path.join(SCRIPT_DIR, ninja_exe)

# A large build (with or without goma) tends to hog all system resources.
# Launching the ninja process with 'nice' priorities improves this situation.
prefix_args = []
if (sys.platform.startswith('linux')
    and os.environ.get('NINJA_BUILD_IN_BACKGROUND', '0') == '1'):
  # nice -10 is process priority 10 lower than default 0
  # ionice -c 3 is IO priority IDLE
  prefix_args = ['nice'] + ['-10']


# Use absolute path for ninja path,
# or fail to execute ninja if depot_tools is not in PATH.
args = prefix_args + [ninja_exe_path] + input_args[1:]

num_cores = multiprocessing.cpu_count()
if not j_specified and not t_specified:
  if use_goma or use_rbe:
    args.append('-j')
    core_multiplier = int(os.environ.get('NINJA_CORE_MULTIPLIER', '40'))
    j_value = num_cores * core_multiplier

    if sys.platform.startswith('win'):
      # On windows, j value higher than 1000 does not improve build performance.
      j_value = min(j_value, 1000)
    elif sys.platform == 'darwin':
      # On Mac, j value higher than 500 causes 'Too many open files' error
      # (crbug.com/936864).
      j_value = min(j_value, 500)

    args.append('%d' % j_value)
  else:
    j_value = num_cores
    # Ninja defaults to |num_cores + 2|
    j_value += int(os.environ.get('NINJA_CORE_ADDITION', '2'))
    args.append('-j')
    args.append('%d' % j_value)

# On Windows, fully quote the path so that the command processor doesn't think
# the whole output is the command.
# On Linux and Mac, if people put depot_tools in directories with ' ',
# shell would misunderstand ' ' as a path separation.
# TODO(yyanagisawa): provide proper quoting for Windows.
# see https://cs.chromium.org/chromium/src/tools/mb/mb.py
for i in range(len(args)):
  if (i == 0 and sys.platform.startswith('win')) or ' ' in args[i]:
    args[i] = '"%s"' % args[i].replace('"', '\\"')

if os.environ.get('NINJA_SUMMARIZE_BUILD', '0') == '1':
  args += ['-d', 'stats']

# If using rbe and the necessary environment variables are set, also start
# reproxy (via bootstrap) before running ninja.
if (not offline and use_rbe and os.path.exists(reclient_bin_dir)
    and os.path.exists(reclient_cfg)):
  setup_args = [
    'RBE_cfg=' + reclient_cfg,
    reclient_bin_dir + '/bootstrap',
    '--re_proxy=' + reclient_bin_dir + '/reproxy']

  teardown_args = [reclient_bin_dir + '/bootstrap', '--shutdown']

  args = setup_args + ['&&'] + args + ['&&'] + teardown_args

if offline and not sys.platform.startswith('win'):
  # Tell goma or reclient to do local compiles. On Windows these environment
  # variables are set by the wrapper batch file.
  print('RBE_remote_disabled=1 GOMA_DISABLED=1 ' + ' '.join(args))
else:
  print(' '.join(args))
