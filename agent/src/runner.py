# coding: utf-8

from logging import basicConfig, DEBUG
from builder_agent.agent import Daemon

if __name__ == "__main__":
    basicConfig(
        level=DEBUG
    )
    build_cmd_mock=(
        "/usr/bin/mockchain -r fedora-21-x86_64 -l /var/tmp/mockremote-x4/build/ "
        "-a https://copr-be.cloud.fedoraproject.org/results/vgologuz/test_copr/fedora-21-x86_64/ "
        "-a https://copr-be.cloud.fedoraproject.org/results/vgologuz/test_copr/fedora-21-x86_64/devel/ "
        "-m '--define=copr_username vgologuz' -m '--define=copr_projectname test_copr' "
        "-m '--define=vendor Fedora Project COPR (vgologuz/test_copr)' "
        "http://miroslav.suchy.cz/copr/copr-ping-1-1.fc20.src.rpm")

    yes_cmd = "yes"
    d = Daemon(
        # build_cmd="wget http://miroslav.suchy.cz/copr/copr-ping-1-1.fc20.src.rpm",
        # build_cmd=yes_cmd
        build_cmd=build_cmd_mock
    )
    d.run()

