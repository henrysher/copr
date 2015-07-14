# coding: utf-8
from shlex import split

import sys
import time
import asyncio
from asyncio import coroutine, Lock, async
from aiohttp import web
from logging import getLogger
from backports.typing import List

log = getLogger(__name__)

running_build_lock = Lock()


class Daemon(object):
    """
    :param build_cmd: string with build command
    :param timeout: max time for build

    """
    def __init__(self, build_cmd: str, timeout: int):
        log.info("Daemon initialized")
        self.loop = asyncio.get_event_loop()

        self.build_cmd = build_cmd
        self.timeout = timeout or 3600 * 6

        self.build_started_on = None
        self.build_ended_on = None
        self.build_stdout = bytearray()
        self.build_stderr = bytearray()
        self.build_return_code = None

        self.timeout_reached_at = None
        self.build_is_running = False
        self.finalize_invoked = False

        self.kill_switch_future = None

    @coroutine
    def start_watched_build(self):
        self.build_is_running = True

        create = asyncio.create_subprocess_exec(
            *split(self.build_cmd),
            limit=127,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        self.build_proc = yield from create
        self.build_started_on = time.time()

        async(self.wait_for_build())
        # since 4.3.3
        # self.kill_switch_future = asyncio.ensure_future(self.kill_switch, loop=self.loop)
        self.kill_switch_future = asyncio.async(self.kill_switch(), loop=self.loop)

    def cancel_ks_safe(self):
        log.info("trying to  cancel KS")
        if self.kill_switch_future:
            self.kill_switch_future.cancel()
            log.info("canceled KS")

    @coroutine
    def kill_switch(self):
        log.info("KS start sleep")
        yield from asyncio.sleep(self.timeout)
        log.info("KS activated -- timeout occurred")
        yield from self.finalize()

    @coroutine
    def wait_for_build(self):
        log.info("Before wait for")
        # try:
        # we have a problem here - if coroutine is cancelled, underlying `yield from proc.communicate()`
        #   would block stdout.read(), so we implement external timeout which calls self.finalize
        # yield from asyncio.wait_for(self.read_result(), self.timeout)
        yield from self.read_result()
        # except asyncio.TimeoutError:
        #     log.exception("On timeout error")
        #     self.build_ended_on = self.timeout_reached_at = time.time()
        #     yield from self.read_rest()

        log.info("After wait for")
        # import ipdb; ipdb.set_trace()
        if not self.finalize_invoked:
            yield from self.finalize()

    # @coroutine
    # def read_rest(self):
    #     log.info("try to read rest")
    #     tmp_stdout = yield from self.build_proc.stdout.read()
    #     log.info("tmp stdout: {}".format(tmp_stdout))

    @coroutine
    def read_result(self):
        log.info("wait for result")
        if self.build_proc and not self.build_proc.stdout.at_eof():
            self.build_stdout, self.build_stderr = yield from self.build_proc.communicate()
        log.info("yielded from communicate")
        self.cancel_ks_safe()

        # could be continued after timeout & terminate invoked
        # if not self.build_return_code and self.build_proc:
        #     self.build_return_code = self.build_proc.returncode

        log.info("Build finished: {}".format(self.build_return_code))
        self.build_ended_on = time.time()

    @coroutine
    def finalize(self):
        self.cancel_ks_safe()

        self.finalize_invoked = True
        log.info("Finalize invoked")
        print(repr(self.build_proc))


        try:
            self.build_proc.terminate()
            self.build_return_code = yield from self.build_proc.wait()
        except Exception:
            log.exception("err waiting")

        log.info("Finalize invoked - wait done")
        self.build_proc = None
        self.build_is_running = False

    @coroutine
    def cancel_build(self, request):
        log.info("< cancel")
        if not self.build_proc:
            return web.Response(body=b"No running build to cancel", status=400)
        else:
            yield from self.finalize()
            return web.Response(body=b"No running build to cancel", status=400)

    @coroutine
    def start_build(self, request):
        log.info("< start")
        if self.build_is_running:
            return web.Response(body=b"Build is running\n", status=400)

        self.reset_results()

        with (yield from running_build_lock):
            yield from self.start_watched_build()
            log.info("Build started")
            text = "Build started\n"
            return web.Response(body=text.encode('utf-8'))

    # @coroutine
    def status(self, request):
        log.info("< status")
        if self.build_started_on is None:
            text = "No build started yet\n"
        elif self.build_ended_on is not None:
            text = "Build finished in {} seconds\n".format(self.build_ended_on - self.build_started_on)
        else:
            text = "Time elapsed: {}, latest build was started at: {}\n"\
                .format(time.time() - self.build_started_on, self.build_started_on)

        try:
            text += "stdout: {}\n".format(len(self.build_stdout)) + bytes(self.build_stdout).decode("utf-8")
        except Exception:
            log.exception("failed add stdout to output")

        try:
            text += "stderr:\n" + bytes(self.build_stderr).decode("utf-8")
        except Exception:
            log.exception("failed add stderr to output")

        # import ipdb; ipdb.set_trace()
        # if self.build_protocol:
        #     text += "output: {}".format(self.build_protocol.stdout)

        # if self.build_return_code:
        #     text += "return code: {}".format(self.build_return_code)
        # if self.build_stdout:
        #     text += "stdout: {}".format(self.build_stdout)
        # if self.build_stderr:
        #     text += "stderr: {}".format(self.build_stderr)

        return web.Response(body=text.encode('utf-8'))

    @coroutine
    def init(self):
        app = web.Application(loop=self.loop)
        app.router.add_route('GET', '/start', self.start_build)
        app.router.add_route('GET', '/status', self.status)
        app.router.add_route('GET', '/cancel', self.cancel_build)

        srv = yield from self.loop.create_server(app.make_handler(), '127.0.0.1', 8080)
        print("Server started at http://127.0.0.1:8080")
        return srv

    def attach_error_handler(self):

        def mute_err_on_terminate(loop, context):
            if 'Exception in callback SubprocessStreamProtocol.process_exited()' == context.get("message"):
                # see: https://bugs.python.org/issue23140
                log.info("muted err")
            else:
                loop.default_exception_handler(context)

        self.loop.set_exception_handler(mute_err_on_terminate)

    def run(self):
        self.attach_error_handler()
        self.loop.run_until_complete(self.init())
        self.loop.run_forever()

    def reset_results(self):
        self.build_started_on = None
        self.build_stdout = bytearray()
        self.build_stderr = bytearray()
        self.build_return_code = None
        self.finalize_invoked = False
