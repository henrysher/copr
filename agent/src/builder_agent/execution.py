# coding: utf-8

from asyncio import coroutine, create_subprocess_exec
import asyncio
from backports.typing import List

@coroutine
def start_watched_build(build_cmd_args: List[str], period: int, timeout: int):
    process = yield from create_subprocess_exec(build_cmd_args,
                                                stdout=asyncio.subprocess.PIPE)

    while not process.returncode:  # not finished
        mb_new_data = process.stdout.read()

