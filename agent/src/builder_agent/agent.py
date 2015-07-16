# coding: utf-8
from shlex import split

import os
import sys
import time
import asyncio
from asyncio import coroutine, Lock, async, create_subprocess_exec
from aiohttp import web
from logging import getLogger
from backports.typing import List

log = getLogger(__name__)

BUFFER_SIZE = 4096


class BuildContext(object):

    def __init__(self):
        self.started_on = None
        self.ended_on = None
        self.cancelled_on = None

        self.timeout_exceeded = False

        self.stdout = bytearray()
        self.stderr = bytearray()
        self.return_code = None

        self.stdout_done_future = asyncio.Future()
        self.stderr_done_future = asyncio.Future()

    @property
    def status(self):
        if self.started_on is None:
            return "ready"
        elif self.cancelled_on is not None:
            return "cancelled"
        elif self.ended_on is not None:
            return "finished"
        else:
            return "running"

    def __str__(self):
        text = self.status + "\n"
        if self.timeout_exceeded:
            text += "WARNING: timeout exceeded\n"
        if self.return_code:
            text += "return_code: {}\n".format(self.return_code)
        try:
            text += "stdout length: {}:\n".format(len(self.stdout)) + bytes(self.stdout).decode("utf-8")
        except Exception:
            log.exception("failed add stdout to output")

        try:
            text += "stderr length: {}:\n".format(len(self.stderr)) + bytes(self.stderr).decode("utf-8")
        except Exception:
            log.exception("failed add stderr to output")

        return text


class Daemon(object):
    """
    :param build_cmd: string with build command
    :param timeout: max time for build

    """
    def __init__(self, build_cmd: str, timeout: int):
        log.info("Daemon initialized")
        log.info("CMD to exec:\n{}".format(build_cmd))
        self.loop = asyncio.get_event_loop()

        self.cmd = build_cmd
        self.timeout = timeout or 3600 * 6

        self.proc = None
        self.build = BuildContext()

        self.build_is_running = False
        self.finalize_invoked = False

        self.kill_switch_task = None
        self.stdout_read_task = None
        self.stderr_read_task = None

    @coroutine
    def kill_switch(self):
        log.info("started kill switch")
        yield from asyncio.sleep(self.timeout)
        log.info("kill switch activated")
        if self.build.status == "running":
            self.build.timeout_exceeded = True
            asyncio.async(self.finalize())

    @coroutine
    def start_watched_build(self):
        self.build_is_running = True

        self.proc = yield from create_subprocess_exec(
            *split(self.cmd),
            env=os.environ.copy(),  # or we just need to set PYTHONUBUFFERED=true
            # limit=127,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        self.build.started_on = time.time()
        async(self.wait_for_build())
        self.kill_switch_task = async(self.kill_switch())
        # todo: after python 4.3.3 user asyncio.ensure_future

    @coroutine
    def read_stream(self, stream: asyncio.StreamReader, target, on_done_future: asyncio.Future, name=None):
        name = name or "stream"
        while self.proc and self.proc.returncode is None and not self.finalize_invoked:
            if stream.at_eof():
                break
            log.info("trying to read from {}".format(name))
            b_data = yield from stream.read(BUFFER_SIZE)
            if b_data:
                target.extend(b_data)
                log.info("read {} bytes from {}".format(len(b_data), name))

            if len(b_data) == BUFFER_SIZE:
                yield from asyncio.sleep(0.0001)
            elif len(b_data) > 0:
                yield from asyncio.sleep(0.02)
            else:
                yield from asyncio.sleep(0.1)
        if not on_done_future.done():
            log.info("set future {} done".format(name))
            on_done_future.set_result(True)

    @coroutine
    def wait_for_build(self):
        log.debug("Wait for result")

        self.stdout_read_task = asyncio.async(self.read_stream(
            self.proc.stdout, self.build.stdout, self.build.stdout_done_future, "stdout"))
        self.stderr_read_task = asyncio.async(self.read_stream(
            self.proc.stderr, self.build.stderr, self.build.stderr_done_future, "stderr"))

        try:
            yield from self.build.stdout_done_future
            yield from self.build.stderr_done_future
        except asyncio.CancelledError:
            log.info("got cancelled error, it's fine")
        except Exception as err:
            log.exception(err)
        log.info("Yielded stdout/stderr")
        yield from self.finalize()

    @coroutine
    def finalize(self):
        if self.finalize_invoked:
            log.info("Called finalize twice or more")
            return

        self.finalize_invoked = True
        log.info("Finalize invoked for child: '{}'".format(self.proc))
        for task in [self.kill_switch_task, self.stderr_read_task, self.stdout_read_task]:
            if task:
                task.cancel()

        for read_future in [self.build.stdout_done_future, self.build.stderr_done_future]:
            if not read_future.done():
                log.info("cancelling future")
                read_future.cancel()

        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass # usually means that process is already halt
        except Exception as err:
            log.exception("err terminate")

        try:
            self.build.return_code = yield from self.proc.wait()
        except Exception:
            log.exception("err waiting")

        log.info("Finalize invoked - wait done")
        self.proc = None

        log.info("Build finished: {}".format(self.build.return_code))
        self.build.ended_on = time.time()

        self.build_is_running = False

    @coroutine
    def cancel_build(self, request):
        log.info("< cancel")
        if not self.proc:
            return web.Response(body=b"No running build to cancel", status=400)
        else:
            self.build.cancelled_on = time.time()
            yield from self.finalize()
            return web.Response(body=b"Cancelling build", status=200)

    @coroutine
    def start_build(self, request):

        if self.build_is_running:
            log.info("< start REJECTED")
            return web.Response(body=b"Build is running\n", status=400)

        log.info("< start EXECUTING")
        self.reset_results()

        # this app should be single threaded, at least the loop with http handler
        # so we don't need to use Lock to start new build
        yield from self.start_watched_build()
        log.info("Build started")
        text = "Build started\n"
        return web.Response(body=text.encode('utf-8'))

    def status(self, request):
        """
        For debug purpose
        """
        log.info("< status")
        return web.Response(body=str(self.build).encode('utf-8'))

    @coroutine
    def init(self):
        app = web.Application(loop=self.loop)
        app.router.add_route('GET', '/start', self.start_build)
        app.router.add_route('GET', '/status', self.status)
        app.router.add_route('GET', '/cancel', self.cancel_build)

        srv = yield from self.loop.create_server(app.make_handler(), '127.0.0.1', 8080)
        print("Server started at http://127.0.0.1:8080")
        return srv

    # def attach_error_handler(self):
    #     def mute_err_on_terminate(loop, context):
    #         if 'Exception in callback SubprocessStreamProtocol.process_exited()' == context.get("message"):
    #             # see: https://bugs.python.org/issue23140
    #             log.info("muted err")
    #         else:
    #             loop.default_exception_handler(context)
    #
    #     self.loop.set_exception_handler(mute_err_on_terminate)

    def run(self):
        # self.attach_error_handler()
        self.loop.run_until_complete(self.init())
        self.loop.run_forever()

    def reset_results(self):
        self.build = BuildContext()

        self.build_is_running = False
        self.finalize_invoked = False
