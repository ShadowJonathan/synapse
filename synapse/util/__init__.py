# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import re
from typing import Callable, Iterable, Optional, Tuple, Union

import attr

from twisted.internet import defer, task

from synapse.logging import context

logger = logging.getLogger(__name__)


def _reject_invalid_json(val):
    """Do not allow Infinity, -Infinity, or NaN values in JSON."""
    raise ValueError("Invalid JSON value: '%s'" % val)


# Create a custom encoder to reduce the whitespace produced by JSON encoding and
# ensure that valid JSON is produced.
json_encoder = json.JSONEncoder(allow_nan=False, separators=(",", ":"))

# Create a custom decoder to reject Python extensions to JSON.
json_decoder = json.JSONDecoder(parse_constant=_reject_invalid_json)


def unwrapFirstError(failure):
    # defer.gatherResults and DeferredLists wrap failures.
    failure.trap(defer.FirstError)
    return failure.value.subFailure


@attr.s(slots=True)
class Clock:
    """
    A Clock wraps a Twisted reactor and provides utilities on top of it.

    Args:
        reactor: The Twisted reactor to use.
    """

    _reactor = attr.ib()

    @defer.inlineCallbacks
    def sleep(self, seconds):
        d = defer.Deferred()
        with context.PreserveLoggingContext():
            self._reactor.callLater(seconds, d.callback, seconds)
            res = yield d
        return res

    def time(self):
        """Returns the current system time in seconds since epoch."""
        return self._reactor.seconds()

    def time_msec(self):
        """Returns the current system time in milliseconds since epoch."""
        return int(self.time() * 1000)

    def looping_call(self, f, msec, *args, **kwargs):
        """Call a function repeatedly.

        Waits `msec` initially before calling `f` for the first time.

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            f(function): The function to call repeatedly.
            msec(float): How long to wait between calls in milliseconds.
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        call = task.LoopingCall(f, *args, **kwargs)
        call.clock = self._reactor
        d = call.start(msec / 1000.0, now=False)
        d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        return call

    def call_later(self, delay, callback, *args, **kwargs):
        """Call something later

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            delay(float): How long to wait in seconds.
            callback(function): Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """

        def wrapped_callback(*args, **kwargs):
            with context.PreserveLoggingContext():
                callback(*args, **kwargs)

        with context.PreserveLoggingContext():
            return self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)

    def cancel_call_later(self, timer, ignore_errs=False):
        try:
            timer.cancel()
        except Exception:
            if not ignore_errs:
                raise


def log_failure(failure, msg, consumeErrors=True):
    """Creates a function suitable for passing to `Deferred.addErrback` that
    logs any failures that occur.

    Args:
        msg (str): Message to log
        consumeErrors (bool): If true consumes the failure, otherwise passes
            on down the callback chain

    Returns:
        func(Failure)
    """

    logger.error(
        msg, exc_info=(failure.type, failure.value, failure.getTracebackObject())
    )

    if not consumeErrors:
        return failure


def glob_to_regex(glob):
    """Converts a glob to a compiled regex object.

    The regex is anchored at the beginning and end of the string.

    Args:
        glob (str)

    Returns:
        re.RegexObject
    """
    res = ""
    for c in glob:
        if c == "*":
            res = res + ".*"
        elif c == "?":
            res = res + "."
        else:
            res = res + re.escape(c)

    # \A anchors at start of string, \Z at end of string
    return re.compile(r"\A" + res + r"\Z", re.IGNORECASE)


PATH_ARG = Union[str, Iterable[Union[str, Tuple[str, dict]]]]


def servelet(cls):
    """@servelet makes the class ready for @on decorations, and automatically adds these to .register() when
    detected.

    Please note that this function ignores any superclass .register functions once @on-decorated methods are found,
    any .register functions found in the defining class will be ran *before* the @on-decorated methods are
    registered."""
    methods = set()

    for name, method in cls.__dict__.items():
        if callable(method) and hasattr(method, "_on"):
            methods.add(method)

    if len(methods) > 0:
        setattr(
            cls, "register", register_proxy(methods, cls.__dict__.get("register", None))
        )

    return cls


def register_proxy(methods: set, orig_register: Optional[Callable] = None):
    def register(self, http_server):

        for _method in methods:
            on = _method._on  # type: Tuple[str, PATH_ARG, dict]

            # Bind method to class instance
            method = _method.__get__(self, self.__class__)

            if isinstance(on[1], str):
                do_register(self.__class__, http_server, on[0], on[1], on[2], method)
            elif isinstance(on[1], Iterable):
                for path_aggregate in on[1]:
                    if isinstance(path_aggregate, str):
                        do_register(
                            self.__class__,
                            http_server,
                            on[0],
                            path_aggregate,
                            on[2],
                            method,
                        )
                    elif isinstance(path_aggregate, tuple):
                        do_register(
                            self.__class__,
                            http_server,
                            on[0],
                            path_aggregate[0],
                            {**on[2], **path_aggregate[1]},
                            method,
                        )

    if orig_register is None:
        return register
    else:

        def bridge(self, http_server):
            register(self, http_server)
            orig_register(self, http_server)

        return bridge


RE_SUGAR = re.compile(r"{(\w+)}")


def transform_path(orig_path) -> str:
    return RE_SUGAR.sub(r"(?P<\1>[^/]*)", orig_path)


def do_register(
    cls, http_server, http_method, client_pattern, client_pattern_kwargs, method
):
    # fixme: circumventing circular import
    from synapse.rest.client.v2_alpha._base import client_patterns

    client_pattern_kwargs.setdefault("add_stopper", True)

    http_server.register_paths(
        http_method,
        client_patterns(transform_path(client_pattern), **client_pattern_kwargs),
        method,
        cls.__name__,
    )


class _On:
    def __init__(self, on_method: str, path_arg: PATH_ARG, **client_patterns_kwargs):
        def _internal(method):
            method._on = (on_method, path_arg, client_patterns_kwargs)
            return method

        self.function = _internal

    def __call__(self, method):
        return self.function(method)


class on(_On):
    @classmethod
    def get(cls, path_arg: PATH_ARG, **client_patterns_kwargs):
        return _On("GET", path_arg, **client_patterns_kwargs)

    @classmethod
    def post(cls, path_arg: PATH_ARG, **client_patterns_kwargs):
        return _On("POST", path_arg, **client_patterns_kwargs)

    @classmethod
    def put(cls, path_arg: PATH_ARG, **client_patterns_kwargs):
        return _On("PUT", path_arg, **client_patterns_kwargs)

    @classmethod
    def options(cls, path_arg: PATH_ARG, **client_patterns_kwargs):
        return _On("OPTIONS", path_arg, **client_patterns_kwargs)

    @classmethod
    def delete(cls, path_arg: PATH_ARG, **client_patterns_kwargs):
        return _On("DELETE", path_arg, **client_patterns_kwargs)
