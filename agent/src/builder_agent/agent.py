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
shared_state = {
    "build_id": 123455
}


class Daemon(object):
    """
    :param build_cmd: string with build command
    :param timeout: max time for build

    """
    def __init__(self, build_cmd: str):
        log.info("Daemon initialized")
        self.loop = asyncio.get_event_loop()

        self.build_cmd = build_cmd

        self.build_started_on = None
        self.build_ended_on = None
        self.build_stdout = bytearray()
        self.build_stderr = bytearray()
        self.build_return_code = None

        self.exit_future = None

    @coroutine
    def start_watched_build(self):
        self.exit_future = asyncio.Future(loop=self.loop)
        create = asyncio.create_subprocess_exec(
            *split(self.build_cmd), stdout=asyncio.subprocess.PIPE)
        self.build_proc = yield from create
        self.build_started_on = time.time()

        async(self.watcher())

    @coroutine
    def watcher(self):
        while self.build_proc and not self.build_proc.stdout.at_eof():
            log.debug("trying to read stdout ...")
            mb_new_data = yield from self.build_proc.stdout.readline()

            if mb_new_data:
                log.debug("got chunk: {}".format(bytes(mb_new_data).decode("utf-8")))
                self.build_stdout.extend(mb_new_data)
            else:
                yield from asyncio.sleep(1)

        # import ipdb; ipdb.set_trace()
        log.info("Build finished: {}".format(self.build_proc.returncode))
        self.build_ended_on = time.time()

    @coroutine
    def finalize(self):
        yield from self.build_proc.terminate()
        yield from self.build_proc.wait()
        self.build_proc = None

    @coroutine
    def cancel_build(self):
        if not self.build_proc:
            return web.Response(body="No running build to cancel", status=400)
        else:
            # yield from self.build_transport.terminate()
            yield from self.finalize()
            return web.Response(body="No running build to cancel", status=400)

    @coroutine
    def start_build(self, request):
        if running_build_lock.locked():
            return web.Response(body="Build {} is running\n"
                                .format(shared_state["build_id"]).encode("utf-8"),
                                status=400)

        self.reset_results()

        with (yield from running_build_lock):
            yield from self.start_watched_build()
            log.info("Build started")
            text = "Build started\n"
            return web.Response(body=text.encode('utf-8'))

    # @coroutine
    def status(self, request):
        if self.build_started_on is None:
            text = "No build started yet\n"
        elif self.build_ended_on is not None:
            text = "Build finished in {} seconds\n".format(self.build_ended_on - self.build_started_on)
        else:
            text = "Time elapsed: {}, latest build was started at: {}\n"\
                .format(time.time() - self.build_started_on, self.build_started_on)

        try:
            text += bytes(self.build_stdout).decode("utf-8")
        except Exception:
            log.exception("failed add stdout to output")

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

        srv = yield from self.loop.create_server(app.make_handler(), '127.0.0.1', 8080)
        print("Server started at http://127.0.0.1:8080")
        return srv

    def run(self):
        self.loop.run_until_complete(self.init())
        self.loop.run_forever()

    def reset_results(self):
        self.build_started_on = None
        self.build_stdout = bytearray()
        self.build_stderr = bytearray()
        self.build_return_code = None
