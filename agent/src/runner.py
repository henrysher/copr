# coding: utf-8

from logging import basicConfig, DEBUG
from builder_agent.agent import Daemon

if __name__ == "__main__":
    basicConfig(
        level=DEBUG,
        format='[%(asctime)s] {%(pathname)s:%(lineno)d}[%(funcName)s] %(levelname)s - %(message)s',
        datefmt='%H:%M:%S',
    )
    build_cmd_mock = (
        "/usr/bin/mockchain -r fedora-21-x86_64 -l /var/tmp/mockremote-x5/build/ "
        "-a https://copr-be.cloud.fedoraproject.org/results/vgologuz/test_copr/fedora-21-x86_64/ "
        "-a https://copr-be.cloud.fedoraproject.org/results/vgologuz/test_copr/fedora-21-x86_64/devel/ "
        "-m '--define=copr_username vgologuz' -m '--define=copr_projectname test_copr' "
        "-m '--define=vendor Fedora Project COPR (vgologuz/test_copr)' "
        "http://miroslav.suchy.cz/copr/copr-ping-1-1.fc20.src.rpm")

    yes_cmd = "yes"

    d = Daemon(
        # build_cmd="wget http://miroslav.suchy.cz/copr/copr-ping-1-1.fc20.src.rpm",
        build_cmd=yes_cmd
        # build_cmd=build_cmd_mock
        , timeout=10
    )
    d.run()

