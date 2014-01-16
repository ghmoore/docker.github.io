from __future__ import print_function
from __future__ import unicode_literals
import logging
import sys
import re
import signal
import sys

from inspect import getdoc

from .. import __version__
from ..project import NoSuchService
from .command import Command
from .formatter import Formatter
from .log_printer import LogPrinter
from .utils import yesno

from docker.client import APIError
from .errors import UserError
from .docopt_command import NoSuchCommand
from .socketclient import SocketClient

log = logging.getLogger(__name__)


def main():
    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(logging.Formatter())
    console_handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(console_handler)
    root_logger.setLevel(logging.DEBUG)

    # Disable requests logging
    logging.getLogger("requests").propagate = False

    try:
        command = TopLevelCommand()
        command.sys_dispatch()
    except KeyboardInterrupt:
        log.error("\nAborting.")
        exit(1)
    except UserError as e:
        log.error(e.msg)
        exit(1)
    except NoSuchService as e:
        log.error(e.msg)
        exit(1)
    except NoSuchCommand as e:
        log.error("No such command: %s", e.command)
        log.error("")
        log.error("\n".join(parse_doc_section("commands:", getdoc(e.supercommand))))
        exit(1)
    except APIError as e:
        log.error(e.explanation)
        exit(1)


# stolen from docopt master
def parse_doc_section(name, source):
    pattern = re.compile('^([^\n]*' + name + '[^\n]*\n?(?:[ \t].*?(?:\n|$))*)',
                         re.IGNORECASE | re.MULTILINE)
    return [s.strip() for s in pattern.findall(source)]


class TopLevelCommand(Command):
    """Punctual, lightweight development environments using Docker.

    Usage:
      fig [options] [COMMAND] [ARGS...]
      fig -h|--help

    Options:
      --verbose            Show more output
      --version            Print version and exit

    Commands:
      build     Build or rebuild services
      help      Get help on a command
      kill      Kill containers
      logs      View output from containers
      ps        List containers
      rm        Remove stopped containers
      run       Run a one-off command
      start     Start services
      stop      Stop services
      up        Create and start containers

    """
    def docopt_options(self):
        options = super(TopLevelCommand, self).docopt_options()
        options['version'] = "fig %s" % __version__
        return options

    def build(self, options):
        """
        Build or rebuild services.

        Services are built once and then tagged as `project_service`,
        e.g. `figtest_db`. If you change a service's `Dockerfile` or the
        contents of its build directory, you can run `fig build` to rebuild it.

        Usage: build [SERVICE...]
        """
        self.project.build(service_names=options['SERVICE'])

    def help(self, options):
        """
        Get help on a command.

        Usage: help COMMAND
        """
        command = options['COMMAND']
        if not hasattr(self, command):
            raise NoSuchCommand(command, self)
        raise SystemExit(getdoc(getattr(self, command)))

    def kill(self, options):
        """
        Force stop service containers.

        Usage: kill [SERVICE...]
        """
        self.project.kill(service_names=options['SERVICE'])

    def logs(self, options):
        """
        View output from containers.

        Usage: logs [SERVICE...]
        """
        containers = self.project.containers(service_names=options['SERVICE'], stopped=True)
        print("Attaching to", list_containers(containers))
        LogPrinter(containers, attach_params={'logs': True}).run()

    def ps(self, options):
        """
        List containers.

        Usage: ps [options] [SERVICE...]

        Options:
            -q    Only display IDs
        """
        containers = self.project.containers(service_names=options['SERVICE'], stopped=True) + self.project.containers(service_names=options['SERVICE'], one_off=True)

        if options['-q']:
            for container in containers:
                print(container.id)
        else:
            headers = [
                'Name',
                'Command',
                'State',
                'Ports',
            ]
            rows = []
            for container in containers:
                rows.append([
                    container.name,
                    container.human_readable_command,
                    container.human_readable_state,
                    container.human_readable_ports,
                ])
            print(Formatter().table(headers, rows))

    def rm(self, options):
        """
        Remove stopped service containers.

        Usage: rm [SERVICE...]
        """
        all_containers = self.project.containers(service_names=options['SERVICE'], stopped=True)
        stopped_containers = [c for c in all_containers if not c.is_running]

        if len(stopped_containers) > 0:
            print("Going to remove", list_containers(stopped_containers))
            if yesno("Are you sure? [yN] ", default=False):
                self.project.remove_stopped(service_names=options['SERVICE'])
        else:
            print("No stopped containers")

    def run(self, options):
        """
        Run a one-off command on a service.

        For example:

            $ fig run web python manage.py shell

        Note that this will not start any services that the command's service
        links to. So if, for example, your one-off command talks to your
        database, you will need to run `fig up -d db` first.

        Usage: run [options] SERVICE COMMAND [ARGS...]

        Options:
            -d    Detached mode: Run container in the background, print new container name
        """
        service = self.project.get_service(options['SERVICE'])
        container_options = {
            'command': [options['COMMAND']] + options['ARGS'],
            'tty': not options['-d'],
            'stdin_open': not options['-d'],
        }
        container = service.create_container(one_off=True, **container_options)
        if options['-d']:
            service.start_container(container, ports=None)
            print(container.name)
        else:
            with self._attach_to_container(
                container.id,
                interactive=True,
                logs=True,
                raw=True
            ) as c:
                service.start_container(container, ports=None)
                c.run()

    def start(self, options):
        """
        Start existing containers.

        Usage: start [SERVICE...]
        """
        self.project.start(service_names=options['SERVICE'])

    def stop(self, options):
        """
        Stop running containers without removing them.

        They can be started again with `fig start`.

        Usage: stop [SERVICE...]
        """
        self.project.stop(service_names=options['SERVICE'])

    def up(self, options):
        """
        Build, (re)create, start and attach to containers for a service.

        By default, `fig up` will aggregate the output of each container, and
        when it exits, all containers will be stopped. If you run `fig up -d`,
        it'll start the containers in the background and leave them running.

        If there are existing containers for a service, `fig up` will stop
        and recreate them (preserving mounted volumes with volumes-from),
        so that changes in `fig.yml` are picked up.

        Usage: up [options] [SERVICE...]

        Options:
            -d    Detached mode: Run containers in the background, print new
                  container names
        """
        detached = options['-d']

        (old, new) = self.project.recreate_containers(service_names=options['SERVICE'])

        if not detached:
            to_attach = [c for (s, c) in new]
            print("Attaching to", list_containers(to_attach))
            log_printer = LogPrinter(to_attach)

        for (service, container) in new:
            service.start_container(container)

        for (service, container) in old:
            container.remove()

        if not detached:
            try:
                log_printer.run()
            finally:
                def handler(signal, frame):
                    self.project.kill(service_names=options['SERVICE'])
                    sys.exit(0)
                signal.signal(signal.SIGINT, handler)

                print("Gracefully stopping... (press Ctrl+C again to force)")
                self.project.stop(service_names=options['SERVICE'])

    def _attach_to_container(self, container_id, interactive, logs=False, stream=True, raw=False):
        stdio = self.client.attach_socket(
            container_id,
            params={
                'stdin': 1 if interactive else 0,
                'stdout': 1,
                'stderr': 0,
                'logs': 1 if logs else 0,
                'stream': 1 if stream else 0
            },
            ws=True,
        )

        stderr = self.client.attach_socket(
            container_id,
            params={
                'stdin': 0,
                'stdout': 0,
                'stderr': 1,
                'logs': 1 if logs else 0,
                'stream': 1 if stream else 0
            },
            ws=True,
        )

        return SocketClient(
            socket_in=stdio,
            socket_out=stdio,
            socket_err=stderr,
            raw=raw,
        )

def list_containers(containers):
    return ", ".join(c.name for c in containers)
