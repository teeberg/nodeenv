#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    nodeenv
    ~~~~~~~
    Node.js virtual environment

    :copyright: (c) 2014 by Eugene Kalinin
    :license: BSD, see LICENSE for more details.
"""

import contextlib
import io
import sys
import os
import re
import stat
import logging
import operator
import optparse
import subprocess
import tarfile
import pipes
import platform
import zipfile
import shutil
import glob

try:  # pragma: no cover (py2 only)
    from ConfigParser import SafeConfigParser as ConfigParser
    from HTMLParser import HTMLParser
    import urllib2
    iteritems = operator.methodcaller('iteritems')
except ImportError:  # pragma: no cover (py3 only)
    from configparser import ConfigParser
    from html.parser import HTMLParser
    import urllib.request as urllib2
    iteritems = operator.methodcaller('items')

from pkg_resources import parse_version

nodeenv_version = '1.1.2'

join = os.path.join
abspath = os.path.abspath
src_domain = "nodejs.org"

is_PY3 = sys.version_info[0] == 3
if is_PY3:
    from functools import cmp_to_key

is_WIN = platform.system() == 'Windows'


# ---------------------------------------------------------
# Utils


# https://github.com/jhermann/waif/blob/master/python/to_uft8.py
def to_utf8(text):
    """Convert given text to UTF-8 encoding (as far as possible)."""
    if not text or is_PY3:
        return text

    try:            # unicode or pure ascii
        return text.encode("utf8")
    except UnicodeDecodeError:
        try:        # successful UTF-8 decode means it's pretty sure UTF-8
            text.decode("utf8")
            return text
        except UnicodeDecodeError:
            try:    # get desperate; and yes,
                    # this has a western hemisphere bias
                return text.decode("cp1252").encode("utf8")
            except UnicodeDecodeError:
                pass

    return text     # return unchanged, hope for the best


class Config(object):
    """
    Configuration namespace.
    """

    # Defaults
    node = 'latest'
    npm = 'latest'
    with_npm = True if is_WIN else False
    jobs = '2'
    without_ssl = False
    debug = False
    profile = False
    make = 'make'
    prebuilt = True

    @classmethod
    def _load(cls, configfiles, verbose=False):
        """
        Load configuration from the given files in reverse order,
        if they exist and have a [nodeenv] section.
        """
        for configfile in reversed(configfiles):
            configfile = os.path.expanduser(configfile)
            if not os.path.exists(configfile):
                continue

            ini_file = ConfigParser()
            ini_file.read(configfile)
            section = "nodeenv"
            if not ini_file.has_section(section):
                continue

            for attr, val in iteritems(vars(cls)):
                if attr.startswith('_') or not \
                   ini_file.has_option(section, attr):
                    continue

                if isinstance(val, bool):
                    val = ini_file.getboolean(section, attr)
                else:
                    val = ini_file.get(section, attr)

                if verbose:
                    print('CONFIG {0}: {1} = {2}'.format(
                        os.path.basename(configfile), attr, val))
                setattr(cls, attr, val)

    @classmethod
    def _dump(cls):
        """
        Print defaults for the README.
        """
        print("    [nodeenv]")
        print("    " + "\n    ".join(
            "%s = %s" % (k, v) for k, v in sorted(iteritems(vars(cls)))
            if not k.startswith('_')))


Config._default = dict(
    (attr, val) for attr, val in iteritems(vars(Config))
    if not attr.startswith('_')
)


def clear_output(out):
    """
    Remove new-lines and
    """
    return out.decode('utf-8').replace('\n', '')


def remove_env_bin_from_path(env, env_bin_dir):
    """
    Remove bin directory of the current environment from PATH
    """
    return env.replace(env_bin_dir + ':', '')


def node_version_from_opt(opt):
    """
    Parse the node version from the optparse options
    """
    if opt.node == 'system':
        out, err = subprocess.Popen(
            ["node", "--version"], stdout=subprocess.PIPE).communicate()
        return parse_version(clear_output(out).replace('v', ''))

    return parse_version(opt.node)


def create_logger():
    """
    Create logger for diagnostic
    """
    # create logger
    logger = logging.getLogger("nodeenv")
    logger.setLevel(logging.INFO)

    # monkey patch
    def emit(self, record):
        msg = self.format(record)
        fs = "%s" if getattr(record, "continued", False) else "%s\n"
        self.stream.write(fs % to_utf8(msg))
        self.flush()
    logging.StreamHandler.emit = emit

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    # create formatter
    formatter = logging.Formatter(fmt="%(message)s")

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    return logger


logger = create_logger()


def parse_args(check=True):
    """
    Parses command line arguments.

    Set `check` to False to skip validation checks.
    """
    parser = optparse.OptionParser(
        version=nodeenv_version,
        usage="%prog [OPTIONS] ENV_DIR")

    parser.add_option(
        '-n', '--node', dest='node', metavar='NODE_VER', default=Config.node,
        help='The node.js version to use, e.g., '
        '--node=0.4.3 will use the node-v0.4.3 '
        'to create the new environment. '
        'The default is last stable version (`latest`). '
        'Use `system` to use system-wide node.')

    parser.add_option(
        '-i', '--iojs',
        action='store_true', dest='io', default=False,
        help='Use iojs instead of nodejs.')

    if not is_WIN:
        parser.add_option(
            '-j', '--jobs', dest='jobs', default=Config.jobs,
            help='Sets number of parallel commands at node.js compilation. '
            'The default is 2 jobs.')

        parser.add_option(
            '--load-average', dest='load_average',
            help='Sets maximum load average for executing parallel commands '
            'at node.js compilation.')

        parser.add_option(
            '--without-ssl', dest='without_ssl',
            action='store_true', default=Config.without_ssl,
            help='Build node.js without SSL support')

        parser.add_option(
            '--debug', dest='debug',
            action='store_true', default=Config.debug,
            help='Build debug variant of the node.js')

        parser.add_option(
            '--profile', dest='profile',
            action='store_true', default=Config.profile,
            help='Enable profiling for node.js')

        parser.add_option(
            '--make', '-m', dest='make_path',
            metavar='MAKE_PATH',
            help='Path to make command',
            default=Config.make)

        parser.add_option(
            '--source', dest='prebuilt',
            action='store_false', default=Config.prebuilt,
            help='Install node.js from the source')

    parser.add_option(
        '-v', '--verbose',
        action='store_true', dest='verbose', default=False,
        help="Verbose mode")

    parser.add_option(
        '-q', '--quiet',
        action='store_true', dest='quiet', default=False,
        help="Quiet mode")

    parser.add_option(
        '-C', '--config-file', dest='config_file', default=None,
        help="Load a different file than '~/.nodeenvrc'. "
        "Pass an empty string for no config (use built-in defaults).")

    parser.add_option(
        '-r', '--requirements',
        dest='requirements', default='', metavar='FILENAME',
        help='Install all the packages listed in the given requirements file.')

    parser.add_option(
        '--prompt', dest='prompt',
        help='Provides an alternative prompt prefix for this environment')

    parser.add_option(
        '-l', '--list', dest='list',
        action='store_true', default=False,
        help='Lists available node.js versions')

    parser.add_option(
        '--update', dest='update',
        action='store_true', default=False,
        help='Install npm packages from file without node')

    parser.add_option(
        '--with-npm', dest='with_npm',
        action='store_true', default=Config.with_npm,
        help='Build without installing npm into the new virtual environment. '
        'Required for node.js < 0.6.3. By default, the npm included with '
        'node.js is used. Under Windows, this defaults to true.')

    parser.add_option(
        '--npm', dest='npm',
        metavar='NPM_VER', default=Config.npm,
        help='The npm version to use, e.g., '
        '--npm=0.3.18 will use the npm-0.3.18.tgz '
        'tarball to install. '
        'The default is last available version (`latest`).')

    parser.add_option(
        '--no-npm-clean', dest='no_npm_clean',
        action='store_true', default=False,
        help='Skip the npm 0.x cleanup.  Cleanup is enabled by default.')

    parser.add_option(
        '--python-virtualenv', '-p', dest='python_virtualenv',
        action='store_true', default=False,
        help='Use current python virtualenv')

    parser.add_option(
        '--clean-src', '-c', dest='clean_src',
        action='store_true', default=False,
        help='Remove "src" directory after installation')

    parser.add_option(
        '--force', dest='force',
        action='store_true', default=False,
        help='Force installation in a pre-existing directory')

    parser.add_option(
        '--prebuilt', dest='prebuilt',
        action='store_true', default=Config.prebuilt,
        help='Install node.js from prebuilt package (default)')

    options, args = parser.parse_args()
    if options.config_file is None:
        options.config_file = ["./setup.cfg", "~/.nodeenvrc"]
    elif not options.config_file:
        options.config_file = []
    else:
        # Make sure that explicitly provided files exist
        if not os.path.exists(options.config_file):
            parser.error("Config file '{0}' doesn't exist!".format(
                options.config_file))
        options.config_file = [options.config_file]

    if not check:
        return options, args

    if not options.list and not options.python_virtualenv:
        if not args:
            parser.error('You must provide a DEST_DIR or '
                         'use current python virtualenv')

        if len(args) > 1:
            parser.error('There must be only one argument: DEST_DIR '
                         '(you gave: {0})'.format(' '.join(args)))

    return options, args


def mkdir(path):
    """
    Create directory
    """
    if not os.path.exists(path):
        logger.debug(' * Creating: %s ... ', path, extra=dict(continued=True))
        os.makedirs(path)
        logger.debug('done.')
    else:
        logger.debug(' * Directory %s already exists', path)


def writefile(dest, content, overwrite=True, append=False):
    """
    Create file and write content in it
    """
    mode_0755 = (stat.S_IRWXU | stat.S_IXGRP |
                 stat.S_IRGRP | stat.S_IROTH | stat.S_IXOTH)
    content = to_utf8(content)
    if is_PY3 and type(content) != bytes:
        content = bytes(content, 'utf-8')
    if not os.path.exists(dest):
        logger.debug(' * Writing %s ... ', dest, extra=dict(continued=True))
        with open(dest, 'wb') as f:
            f.write(content)
        os.chmod(dest, mode_0755)
        logger.debug('done.')
        return
    else:
        with open(dest, 'rb') as f:
            c = f.read()
        if content in c:
            logger.debug(' * Content %s already in place', dest)
            return

        if not overwrite:
            logger.info(' * File %s exists with different content; '
                        ' not overwriting', dest)
            return

        if append:
            logger.info(' * Appending data to %s', dest)
            with open(dest, 'ab') as f:
                if not is_WIN:
                    f.write(DISABLE_PROMPT.encode('utf-8'))
                f.write(content)
                if not is_WIN:
                    f.write(ENABLE_PROMPT.encode('utf-8'))
            return

        logger.info(' * Overwriting %s with new content', dest)
        with open(dest, 'wb') as f:
            f.write(content)


def callit(cmd, show_stdout=True, in_shell=False,
           cwd=None, extra_env=None):
    """
    Execute cmd line in sub-shell
    """
    all_output = []
    cmd_parts = []

    for part in cmd:
        if len(part) > 45:
            part = part[:20] + "..." + part[-20:]
        if ' ' in part or '\n' in part or '"' in part or "'" in part:
            part = '"%s"' % part.replace('"', '\\"')
        cmd_parts.append(part)
    cmd_desc = ' '.join(cmd_parts)
    logger.debug(" ** Running command %s" % cmd_desc)

    if in_shell:
        cmd = ' '.join(cmd)

    # output
    stdout = subprocess.PIPE

    # env
    if extra_env:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
    else:
        env = None

    # execute
    try:
        proc = subprocess.Popen(
            cmd, stderr=subprocess.STDOUT, stdin=None, stdout=stdout,
            cwd=cwd, env=env, shell=in_shell)
    except Exception:
        e = sys.exc_info()[1]
        logger.error("Error %s while executing command %s" % (e, cmd_desc))
        raise

    stdout = proc.stdout
    while stdout:
        line = stdout.readline()
        if not line:
            break
        line = line.decode('utf-8').rstrip()
        all_output.append(line)
        if show_stdout:
            logger.info(line)
    proc.wait()

    # error handler
    if proc.returncode:
        if show_stdout:
            for s in all_output:
                logger.critical(s)
        raise OSError("Command %s failed with error code %s"
                      % (cmd_desc, proc.returncode))

    return proc.returncode, all_output


def get_root_url(version):
    if parse_version(version) > parse_version("0.5.0"):
        return 'https://%s/dist/v%s/' % (src_domain, version)
    else:
        return 'https://%s/dist/' % (src_domain)


def get_node_bin_url(version):
    archmap = {
        'x86':    'x86',  # Windows Vista 32
        'i686':   'x86',
        'x86_64': 'x64',  # Linux Ubuntu 64
        'AMD64':  'x64',  # Windows Server 2012 R2 (x64)
        'armv6l': 'armv6l',     # arm
        'armv7l': 'armv7l',
        'aarch64': 'armv64',
    }
    sysinfo = {
        'system': platform.system().lower(),
        'arch': archmap[platform.machine()],
    }
    if is_WIN:
        filename = 'win-%(arch)s/node.exe' % sysinfo
    else:
        postfix = '-%(system)s-%(arch)s.tar.gz' % sysinfo
        filename = '%s-v%s%s' % (get_binary_prefix(), version, postfix)
    return get_root_url(version) + filename


def get_node_src_url(version):
    tar_name = '%s-v%s.tar.gz' % (get_binary_prefix(), version)
    return get_root_url(version) + tar_name


@contextlib.contextmanager
def tarfile_open(*args, **kwargs):
    """Compatibility layer because py26."""
    tf = tarfile.open(*args, **kwargs)
    try:
        yield tf
    finally:
        tf.close()


def download_node_src(node_url, src_dir, opt, prefix):
    """
    Download source code
    """
    logger.info('.', extra=dict(continued=True))
    dl_contents = io.BytesIO(urlopen(node_url).read())
    logger.info('.', extra=dict(continued=True))

    if is_WIN:
        writefile(join(src_dir, 'node.exe'), dl_contents.read())
    else:
        with tarfile_open(fileobj=dl_contents) as tarfile_obj:
            member_list = tarfile_obj.getmembers()
            extract_list = []
            for member in member_list:
                node_ver = opt.node.replace('.', '\.')
                rexp_string = "%s-v%s[^/]*/(README\.md|CHANGELOG\.md|LICENSE)"\
                    % (prefix, node_ver)
                if re.match(rexp_string, member.name) is None:
                    extract_list.append(member)
            tarfile_obj.extractall(src_dir, extract_list)


def urlopen(url):
    home_url = "https://github.com/ekalinin/nodeenv/"
    headers = {'User-Agent': 'nodeenv/%s (%s)' % (nodeenv_version, home_url)}
    req = urllib2.Request(url, None, headers)
    return urllib2.urlopen(req)

# ---------------------------------------------------------
# Virtual environment functions


def copytree(src, dst, symlinks=False, ignore=None):
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, symlinks, ignore)
        else:
            shutil.copy2(s, d)


def copy_node_from_prebuilt(env_dir, src_dir, node_version):
    """
    Copy prebuilt binaries into environment
    """
    logger.info('.', extra=dict(continued=True))
    prefix = get_binary_prefix()
    if is_WIN:
        src_exe = join(src_dir, 'node.exe')
        dst_exe = join(env_dir, 'Scripts', 'node.exe')
        mkdir(join(env_dir, 'Scripts'))
        callit(['copy', '/Y', '/L', src_exe, dst_exe], False, True)
    else:
        src_folder_tpl = src_dir + to_utf8('/%s-v%s*' % (prefix, node_version))
        for src_folder in glob.glob(src_folder_tpl):
            copytree(src_folder, env_dir)
    logger.info('.', extra=dict(continued=True))


def build_node_from_src(env_dir, src_dir, node_src_dir, opt):
    env = {}
    make_param_names = ['load-average', 'jobs']
    make_param_values = map(
        lambda x: getattr(opt, x.replace('-', '_')),
        make_param_names)
    make_opts = [
        '--{0}={1}'.format(name, value)
        if len(value) > 0 else '--{0}'.format(name)
        for name, value in zip(make_param_names, make_param_values)
        if value is not None
    ]

    if getattr(sys.version_info, 'major', sys.version_info[0]) > 2:
        # Currently, the node.js build scripts are using python2.*,
        # therefore we need to temporarily point python exec to the
        # python 2.* version in this case.
        try:
            _, which_python2_output = callit(
                ['which', 'python2'], opt.verbose, True, node_src_dir, env
            )
            python2_path = which_python2_output[0]
        except (OSError, IndexError):
            raise OSError(
                'Python >=3.0 virtualenv detected, but no python2 '
                'command (required for building node.js) was found'
            )
        logger.debug(' * Temporarily pointing python to %s', python2_path)
        node_tmpbin_dir = join(src_dir, 'tmpbin')
        node_tmpbin_link = join(node_tmpbin_dir, 'python')
        mkdir(node_tmpbin_dir)
        if not os.path.exists(node_tmpbin_link):
            callit(['ln', '-s', python2_path, node_tmpbin_link])
        env['PATH'] = '{}:{}'.format(node_tmpbin_dir,
                                     os.environ.get('PATH', ''))

    conf_cmd = []
    conf_cmd.append('./configure')
    conf_cmd.append('--prefix=%s' % pipes.quote(env_dir))
    if opt.without_ssl:
        conf_cmd.append('--without-ssl')
    if opt.debug:
        conf_cmd.append('--debug')
    if opt.profile:
        conf_cmd.append('--profile')

    make_cmd = opt.make_path

    callit(conf_cmd, opt.verbose, True, node_src_dir, env)
    logger.info('.', extra=dict(continued=True))
    callit([make_cmd] + make_opts, opt.verbose, True, node_src_dir, env)
    logger.info('.', extra=dict(continued=True))
    callit([make_cmd + ' install'], opt.verbose, True, node_src_dir, env)


def get_binary_prefix():
    return to_utf8('node' if src_domain == 'nodejs.org' else 'iojs')


def install_node(env_dir, src_dir, opt):
    """
    Download source code for node.js, unpack it
    and install it in virtual environment.
    """
    env_dir = abspath(env_dir)
    prefix = get_binary_prefix()
    node_src_dir = join(src_dir, to_utf8('%s-v%s' % (prefix, opt.node)))
    src_type = "prebuilt" if opt.prebuilt else "source"

    logger.info(' * Install %s %s (%s) ' % (src_type, prefix, opt.node),
                extra=dict(continued=True))

    if opt.prebuilt:
        node_url = get_node_bin_url(opt.node)
    else:
        node_url = get_node_src_url(opt.node)

    # get src if not downloaded yet
    if not os.path.exists(node_src_dir):
        download_node_src(node_url, src_dir, opt, prefix)

    logger.info('.', extra=dict(continued=True))

    if opt.prebuilt:
        copy_node_from_prebuilt(env_dir, src_dir, opt.node)
    else:
        build_node_from_src(env_dir, src_dir, node_src_dir, opt)

    logger.info(' done.')


def install_npm(env_dir, src_dir, opt):
    """
    Download source code for npm, unpack it
    and install it in virtual environment.
    """
    logger.info(' * Install npm.js (%s) ... ' % opt.npm,
                extra=dict(continued=True))
    npm_contents = urlopen('https://www.npmjs.org/install.sh').read()
    env = dict(
        os.environ,
        clean='no' if opt.no_npm_clean else 'yes',
        npm_install=opt.npm,
    )
    proc = subprocess.Popen(
        (
            'bash', '-c',
            '. {0} && exec bash'.format(
                pipes.quote(join(env_dir, 'bin', 'activate')),
            )
        ),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out, _ = proc.communicate(npm_contents)
    if opt.verbose:
        logger.info(out)
    logger.info('done.')


def install_npm_win(env_dir, src_dir, opt):
    """
    Download source code for npm, unpack it
    and install it in virtual environment.
    """
    logger.info(' * Install npm.js (%s) ... ' % opt.npm,
                extra=dict(continued=True))
    npm_url = 'https://github.com/npm/npm/archive/%s.zip' % opt.npm
    npm_contents = io.BytesIO(urlopen(npm_url).read())

    bin_path = join(env_dir, 'Scripts')
    node_modules_path = join(bin_path, 'node_modules', 'npm')

    if os.path.exists(node_modules_path):
        shutil.rmtree(node_modules_path)

    if os.path.exists(join(bin_path, 'npm.cmd')):
        os.remove(join(bin_path, 'npm.cmd'))

    if os.path.exists(join(bin_path, 'npm-cli.js')):
        os.remove(join(bin_path, 'npm-cli.js'))

    with zipfile.ZipFile(npm_contents, 'r') as zipf:
        zipf.extractall(src_dir)

    npm_ver = 'npm-%s' % opt.npm
    shutil.copytree(join(src_dir, npm_ver), node_modules_path)
    shutil.copy(join(src_dir, npm_ver, 'bin', 'npm.cmd'),
                join(bin_path, 'npm.cmd'))
    shutil.copy(join(src_dir, npm_ver, 'bin', 'npm-cli.js'),
                join(bin_path, 'npm-cli.js'))


def install_packages(env_dir, opt):
    """
    Install node.js packages via npm
    """
    logger.info(' * Install node.js packages ... ',
                extra=dict(continued=True))
    packages = [package.strip() for package in
                open(opt.requirements).readlines()]
    activate_path = join(env_dir, 'bin', 'activate')
    real_npm_ver = opt.npm if opt.npm.count(".") == 2 else opt.npm + ".0"
    if opt.npm == "latest" or real_npm_ver >= "1.0.0":
        cmd = '. ' + pipes.quote(activate_path) + \
              ' && npm install -g %(pack)s'
    else:
        cmd = '. ' + pipes.quote(activate_path) + \
              ' && npm install %(pack)s' + \
              ' && npm activate %(pack)s'

    for package in packages:
        if not package:
            continue
        callit(cmd=[
            cmd % {"pack": package}], show_stdout=opt.verbose, in_shell=True)

    logger.info('done.')


def install_activate(env_dir, opt):
    """
    Install virtual environment activation script
    """
    if is_WIN:
        files = {
            'activate.bat': ACTIVATE_BAT,
            "deactivate.bat": DEACTIVATE_BAT,
            "Activate.ps1": ACTIVATE_PS1
        }
        bin_dir = join(env_dir, 'Scripts')
        shim_node = join(bin_dir, "node.exe")
        shim_nodejs = join(bin_dir, "nodejs.exe")
    else:
        files = {'activate': ACTIVATE_SH, 'shim': SHIM}
        bin_dir = join(env_dir, 'bin')
        shim_node = join(bin_dir, "node")
        shim_nodejs = join(bin_dir, "nodejs")

    if opt.node == "system":
        files["node"] = SHIM

    mod_dir = join('lib', 'node_modules')
    prompt = opt.prompt or '(%s)' % os.path.basename(os.path.abspath(env_dir))

    if opt.node == "system":
        env = os.environ.copy()
        env.update({'PATH': remove_env_bin_from_path(env['PATH'], bin_dir)})
        for candidate in ("nodejs", "node"):
            which_node_output, _ = subprocess.Popen(
                ["which", candidate],
                stdout=subprocess.PIPE, env=env).communicate()
            shim_node = clear_output(which_node_output)
            if shim_node:
                break
        assert shim_node, "Did not find nodejs or node system executable"

    for name, content in files.items():
        file_path = join(bin_dir, name)
        content = content.replace('__NODE_VIRTUAL_PROMPT__', prompt)
        content = content.replace('__NODE_VIRTUAL_ENV__',
                                  os.path.abspath(env_dir))
        content = content.replace('__SHIM_NODE__', shim_node)
        content = content.replace('__BIN_NAME__', os.path.basename(bin_dir))
        content = content.replace('__MOD_NAME__', mod_dir)
        # if we call in the same environment:
        #   $ nodeenv -p --prebuilt
        #   $ nodeenv -p --node=system
        # we should get `bin/node` not as binary+string.
        # `bin/activate` should be appended if we inside
        # existing python's virtual environment
        need_append = 0 if name in ('node', 'shim') else opt.python_virtualenv
        writefile(file_path, content, append=need_append)

    if not os.path.exists(shim_nodejs):
        if is_WIN:
            try:
                callit(['mklink', shim_nodejs, 'node.exe'], True, True)
            except OSError:
                logger.error('Error: Failed to create nodejs.exe link')
        else:
            os.symlink("node", shim_nodejs)


def set_predeactivate_hook(env_dir):
    if not is_WIN:
        with open(join(env_dir, 'bin', 'predeactivate'), 'a') as hook:
            hook.write(PREDEACTIVATE_SH)


def create_environment(env_dir, opt):
    """
    Creates a new environment in ``env_dir``.
    """
    if os.path.exists(env_dir) and not opt.python_virtualenv:
        logger.info(' * Environment already exists: %s', env_dir)
        if not opt.force:
            sys.exit(2)
    src_dir = to_utf8(abspath(join(env_dir, 'src')))
    mkdir(src_dir)

    if opt.node != "system":
        install_node(env_dir, src_dir, opt)
    else:
        mkdir(join(env_dir, 'bin'))
        mkdir(join(env_dir, 'lib'))
        mkdir(join(env_dir, 'lib', 'node_modules'))
    # activate script install must be
    # before npm install, npm use activate
    # for install
    install_activate(env_dir, opt)
    if node_version_from_opt(opt) < parse_version("0.6.3") or opt.with_npm:
        instfunc = install_npm_win if is_WIN else install_npm
        instfunc(env_dir, src_dir, opt)
    if opt.requirements:
        install_packages(env_dir, opt)
    if opt.python_virtualenv:
        set_predeactivate_hook(env_dir)
    # Cleanup
    if opt.clean_src:
        shutil.rmtree(src_dir)


class GetsAHrefs(HTMLParser):
    def __init__(self):
        # Old style class in py2 :(
        HTMLParser.__init__(self)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs).get('href', ''))


VERSION_RE = re.compile('\d+\.\d+\.\d+')


def _py2_cmp(a, b):
    # -1 = a < b, 0 = eq, 1 = a > b
    return (a > b) - (a < b)


def compare_versions(version, other_version):
    version_tuple = version.split('.')
    other_tuple = other_version.split('.')

    version_length = len(version_tuple)
    other_length = len(other_tuple)
    version_dots = min(version_length, other_length)

    for i in range(version_dots):
        a = int(version_tuple[i])
        b = int(other_tuple[i])
        cmp_value = _py2_cmp(a, b)
        if cmp_value != 0:
            return cmp_value

    return _py2_cmp(version_length, other_length)


def get_node_versions():
    response = urlopen('https://{0}/dist'.format(src_domain))
    href_parser = GetsAHrefs()
    href_parser.feed(response.read().decode('UTF-8'))

    versions = set(
        VERSION_RE.search(href).group()
        for href in href_parser.hrefs
        if VERSION_RE.search(href)
    )
    if is_PY3:
        key_compare = cmp_to_key(compare_versions)
        versions = sorted(versions, key=key_compare)
    else:
        versions = sorted(versions, cmp=compare_versions)
    return versions


def print_node_versions():
    """
    Prints into stdout all available node.js versions
    """
    versions = get_node_versions()
    chunks_of_8 = [
        versions[pos:pos + 8] for pos in range(0, len(versions), 8)
    ]
    for chunk in chunks_of_8:
        logger.info('\t'.join(chunk))


def get_last_stable_node_version():
    """
    Return last stable node.js version
    """
    response = urlopen('https://%s/dist/latest/' % (src_domain))
    href_parser = GetsAHrefs()
    href_parser.feed(response.read().decode('UTF-8'))

    links = []
    pattern = re.compile(r'''%s-v([0-9]+)\.([0-9]+)\.([0-9]+)\.tar\.gz''' % (
        get_binary_prefix()))

    for href in href_parser.hrefs:
        match = pattern.match(href)
        if match:
            version = u'.'.join(match.groups())
            major, minor, revision = map(int, match.groups())
            links.append((version, major, minor, revision))
            break

    return links[-1][0]


def get_env_dir(opt, args):
    if opt.python_virtualenv:
        if hasattr(sys, 'real_prefix'):
            res = sys.prefix
        elif hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix:
            res = sys.prefix
        else:
            logger.error('No python virtualenv is available')
            sys.exit(2)
    else:
        res = args[0]
    return to_utf8(res)


def is_installed(name):
    try:
        devnull = open(os.devnull)
        subprocess.Popen([name], stdout=devnull, stderr=devnull)
    except OSError as e:
        if e.errno == os.errno.ENOENT:
            return False
    return True


def main():
    """
    Entry point
    """
    # quick&dirty way to help update the README
    if "--dump-config-defaults" in sys.argv:
        Config._dump()
        return

    opt, args = parse_args(check=False)
    Config._load(opt.config_file, opt.verbose)

    opt, args = parse_args()

    if opt.node.lower() == 'system' and is_WIN:
        logger.error('Installing system node.js on win32 is not supported!')
        exit(1)

    if opt.io:
        global src_domain
        src_domain = "iojs.org"

    if not opt.node or opt.node.lower() == "latest":
        opt.node = get_last_stable_node_version()

    if opt.list:
        print_node_versions()
    elif opt.update:
        env_dir = get_env_dir(opt, args)
        install_packages(env_dir, opt)
    else:
        env_dir = get_env_dir(opt, args)
        create_environment(env_dir, opt)


# ---------------------------------------------------------
# Shell scripts content

DISABLE_PROMPT = """
# disable nodeenv's prompt
# (prompt already changed by original virtualenv's script)
# https://github.com/ekalinin/nodeenv/issues/26
NODE_VIRTUAL_ENV_DISABLE_PROMPT=1
"""

ENABLE_PROMPT = """
unset NODE_VIRTUAL_ENV_DISABLE_PROMPT
"""

SHIM = """#!/usr/bin/env bash
export NODE_PATH=__NODE_VIRTUAL_ENV__/lib/node_modules
export NPM_CONFIG_PREFIX=__NODE_VIRTUAL_ENV__
export npm_config_prefix=__NODE_VIRTUAL_ENV__
exec __SHIM_NODE__ "$@"
"""

ACTIVATE_BAT = """\
@echo off
set "NODE_VIRTUAL_ENV=__NODE_VIRTUAL_ENV__"
if not defined PROMPT (
    set "PROMPT=$P$G"
)
if defined _OLD_VIRTUAL_PROMPT (
    set "PROMPT=%_OLD_VIRTUAL_PROMPT%"
)
if defined _OLD_VIRTUAL_NODE_PATH (
    set "NODE_PATH=%_OLD_VIRTUAL_NODE_PATH%"
)
set "_OLD_VIRTUAL_PROMPT=%PROMPT%"
set "PROMPT=__NODE_VIRTUAL_PROMPT__ %PROMPT%"
if defined NODE_PATH (
    set "_OLD_VIRTUAL_NODE_PATH=%NODE_PATH%"
    set NODE_PATH=
)
if defined _OLD_VIRTUAL_PATH (
    set "PATH=%_OLD_VIRTUAL_PATH%"
) else (
    set "_OLD_VIRTUAL_PATH=%PATH%"
)
set "PATH=%NODE_VIRTUAL_ENV%\Scripts;%PATH%"
:END

"""

DEACTIVATE_BAT = """\
@echo off
if defined _OLD_VIRTUAL_PROMPT (
    set "PROMPT=%_OLD_VIRTUAL_PROMPT%"
)
set _OLD_VIRTUAL_PROMPT=
if defined _OLD_VIRTUAL_NODE_PATH (
    set "NODE_PATH=%_OLD_VIRTUAL_NODE_PATH%"
    set _OLD_VIRTUAL_NODE_PATH=
)
if defined _OLD_VIRTUAL_PATH (
    set "PATH=%_OLD_VIRTUAL_PATH%"
)
set _OLD_VIRTUAL_PATH=
set NODE_VIRTUAL_ENV=
:END
"""

ACTIVATE_PS1 = """\
function global:deactivate ([switch]$NonDestructive) {
    # Revert to original values
    if (Test-Path function:_OLD_VIRTUAL_PROMPT) {
        copy-item function:_OLD_VIRTUAL_PROMPT function:prompt
        remove-item function:_OLD_VIRTUAL_PROMPT
    }
    if (Test-Path env:_OLD_VIRTUAL_NODE_PATH) {
        copy-item env:_OLD_VIRTUAL_NODE_PATH env:NODE_PATH
        remove-item env:_OLD_VIRTUAL_NODE_PATH
    }
    if (Test-Path env:_OLD_VIRTUAL_PATH) {
        copy-item env:_OLD_VIRTUAL_PATH env:PATH
        remove-item env:_OLD_VIRTUAL_PATH
    }
    if (Test-Path env:NODE_VIRTUAL_ENV) {
        remove-item env:NODE_VIRTUAL_ENV
    }
    if (!$NonDestructive) {
        # Self destruct!
        remove-item function:deactivate
    }
}

deactivate -nondestructive
$env:NODE_VIRTUAL_ENV="__NODE_VIRTUAL_ENV__"

# Set the prompt to include the env name
# Make sure _OLD_VIRTUAL_PROMPT is global
function global:_OLD_VIRTUAL_PROMPT {""}
copy-item function:prompt function:_OLD_VIRTUAL_PROMPT
function global:prompt {
    Write-Host -NoNewline -ForegroundColor Green '__NODE_VIRTUAL_PROMPT__ '
    _OLD_VIRTUAL_PROMPT
}

# Clear NODE_PATH
if (Test-Path env:NODE_PATH) {
    copy-item env:NODE_PATH env:_OLD_VIRTUAL_NODE_PATH
    remove-item env:NODE_PATH
}

# Add the venv to the PATH
copy-item env:PATH env:_OLD_VIRTUAL_PATH
$env:PATH = "$env:NODE_VIRTUAL_ENV\Scripts;$env:PATH"
"""

ACTIVATE_SH = """

# This file must be used with "source bin/activate" *from bash*
# you cannot run it directly

deactivate_node () {
    # reset old environment variables
    if [ -n "$_OLD_NODE_VIRTUAL_PATH" ] ; then
        PATH="$_OLD_NODE_VIRTUAL_PATH"
        export PATH
        unset _OLD_NODE_VIRTUAL_PATH

        NODE_PATH="$_OLD_NODE_PATH"
        export NODE_PATH
        unset _OLD_NODE_PATH

        NPM_CONFIG_PREFIX="$_OLD_NPM_CONFIG_PREFIX"
        npm_config_prefix="$_OLD_npm_config_prefix"
        export NPM_CONFIG_PREFIX
        export npm_config_prefix
        unset _OLD_NPM_CONFIG_PREFIX
        unset _OLD_npm_config_prefix
    fi

    # This should detect bash and zsh, which have a hash command that must
    # be called to get it to forget past commands.  Without forgetting
    # past commands the $PATH changes we made may not be respected
    if [ -n "$BASH" -o -n "$ZSH_VERSION" ] ; then
        hash -r
    fi

    if [ -n "$_OLD_NODE_VIRTUAL_PS1" ] ; then
        PS1="$_OLD_NODE_VIRTUAL_PS1"
        export PS1
        unset _OLD_NODE_VIRTUAL_PS1
    fi

    unset NODE_VIRTUAL_ENV
    if [ ! "$1" = "nondestructive" ] ; then
    # Self destruct!
        unset -f deactivate_node
    fi
}

freeze () {
    local NPM_VER=`npm -v | cut -d '.' -f 1`
    local re="[a-zA-Z0-9\.\-]+@[0-9]+\.[0-9]+\.[0-9]+([\+\-][a-zA-Z0-9\.\-]+)*"
    if [ "$NPM_VER" = '0' ]; then
        NPM_LIST=`npm list installed active 2>/dev/null | \
                  cut -d ' ' -f 1 | grep -v npm`
    else
        local npmls="npm ls -g"
        if [ "$1" = "-l" ]; then
            npmls="npm ls"
            shift
        fi
        NPM_LIST=$(eval ${npmls} | grep -E '^.{4}\w{1}'| \
                                   grep -o -E "$re"| grep -v npm)
    fi

    if [ -z "$@" ]; then
        echo "$NPM_LIST"
    else
        echo "$NPM_LIST" > $@
    fi
}

# unset irrelavent variables
deactivate_node nondestructive

# find the directory of this script
# http://stackoverflow.com/a/246128
if [ "${BASH_SOURCE}" ] ; then
    SOURCE="${BASH_SOURCE[0]}"

    while [ -h "$SOURCE" ] ; do SOURCE="$(readlink "$SOURCE")"; done
    DIR="$( command cd -P "$( dirname "$SOURCE" )" > /dev/null && pwd )"

    NODE_VIRTUAL_ENV="$(dirname "$DIR")"
else
    # dash not movable. fix use case:
    #   dash -c " . node-env/bin/activate && node -v"
    NODE_VIRTUAL_ENV="__NODE_VIRTUAL_ENV__"
fi

# NODE_VIRTUAL_ENV is the parent of the directory where this script is
export NODE_VIRTUAL_ENV

_OLD_NODE_VIRTUAL_PATH="$PATH"
PATH="$NODE_VIRTUAL_ENV/lib/node_modules/.bin:$NODE_VIRTUAL_ENV/__BIN_NAME__:$PATH"
export PATH

_OLD_NODE_PATH="$NODE_PATH"
NODE_PATH="$NODE_VIRTUAL_ENV/__MOD_NAME__"
export NODE_PATH

_OLD_NPM_CONFIG_PREFIX="$NPM_CONFIG_PREFIX"
_OLD_npm_config_prefix="$npm_config_prefix"
NPM_CONFIG_PREFIX="$NODE_VIRTUAL_ENV"
npm_config_prefix="$NODE_VIRTUAL_ENV"
export NPM_CONFIG_PREFIX
export npm_config_prefix

if [ -z "$NODE_VIRTUAL_ENV_DISABLE_PROMPT" ] ; then
    _OLD_NODE_VIRTUAL_PS1="$PS1"
    if [ "x__NODE_VIRTUAL_PROMPT__" != x ] ; then
        PS1="__NODE_VIRTUAL_PROMPT__$PS1"
    else
    if [ "`basename \"$NODE_VIRTUAL_ENV\"`" = "__" ] ; then
        # special case for Aspen magic directories
        # see http://www.zetadev.com/software/aspen/
        PS1="[`basename \`dirname \"$NODE_VIRTUAL_ENV\"\``] $PS1"
    else
        PS1="(`basename \"$NODE_VIRTUAL_ENV\"`)$PS1"
    fi
    fi
    export PS1
fi

# This should detect bash and zsh, which have a hash command that must
# be called to get it to forget past commands.  Without forgetting
# past commands the $PATH changes we made may not be respected
if [ -n "$BASH" -o -n "$ZSH_VERSION" ] ; then
    hash -r
fi
"""

PREDEACTIVATE_SH = """
if type -p deactivate_node > /dev/null; then deactivate_node;fi
"""

if __name__ == '__main__':
    main()
