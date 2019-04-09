#!/usr/bin/env python2.7

# oci-utils
#
# Copyright (c) 2018, 2019 Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown
# at http://oss.oracle.com/licenses/upl.

""" OS command line utils.
"""

import logging
import subprocess

from . import SUDO_CMD

__all__ = ['call', 'call_output', 'call_popen_output']

_logger = logging.getLogger('oci-utils.sudo')


def _prepare_command(cmd):
    """
    Prepare the command line to be executed prepend sudo if not alreary present.

    Parameters
    ----------
    cmd : list
        Command line as list of strings.

    Returns
    -------
        list
            The prepared command.
    """
    assert (len(cmd) > 0), 'empty command list'
    _cmd = []
    if cmd[0] != SUDO_CMD:
        _cmd.insert(0, SUDO_CMD)
    _cmd.extend(cmd)

    return _cmd


def call(cmd, log_output=True):
    """
    Execute a command.

    Parameters
    ----------
    cmd: list
        Command line as list of strings.
    log_output: bool
        Write error messages to logfile if set.

    Returns
    -------
        int
            The command return code.
    """
    _c = _prepare_command(cmd)
    try:
        subprocess.check_call(_c, stderr=subprocess.STDOUT)
    except OSError:
        return 404
    except subprocess.CalledProcessError as e:
        if log_output:
            _logger.debug("Error executing {}: {}\n{}\n"
                          .format(_c, e.returncode, e.output))
        return e.returncode
    return 0


def call_output(cmd, log_output=True):
    """
    Executes a command.

    Parameters
    ----------
    cmd: list
        Command line as list of strings.
    log_output: bool
        Write error messages to logfile if set.

    Returns
    -------
        str
            The stdout and stderr, on success.
        int
            404 on OSError.
        None
            When command execution fails.
    """
    _c = _prepare_command(cmd)
    try:
        return subprocess.check_output(_c, stderr=subprocess.STDOUT)
    except OSError:
        return 404
    except subprocess.CalledProcessError as e:
        if log_output:
            _logger.debug("Error execeuting {}: {}\n{}\n"
                          .format(_c, e.returncode, e.output))
        return None


def call_popen_output(cmd, log_output=True):
    """
    Executes a command.

    Parameters
    ----------
    cmd: list
        Command line as list of strings.
    log_output: bool
        Write error messages to logfile if set.

    Returns
    -------
        str
            The stdout and stderr, on success.
        int
            404 on OSError.
        None
            When command execution fails.
    """
    _c = _prepare_command(cmd)
    try:
        p = subprocess.Popen(' '.join(_c), shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        return p.communicate()[0]
    except OSError:
        return 404
    except subprocess.CalledProcessError as e:
        if log_output:
            _logger.debug("Error execeuting {}: {}\n{}\n"
                          .format(_c, e.returncode, e.output))
        return None


def delete_file(path):
    """
    Delete a file.

    Parameters
    ----------
    path: str
        The full path of the file.

    Returns
    -------
        The return code fo the delete command.
    """
    return call(['/bin/rm', '-f', path])
