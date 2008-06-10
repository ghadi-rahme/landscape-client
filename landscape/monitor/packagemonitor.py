import logging
import sys
import os

from twisted.internet.utils import getProcessOutput

from landscape.package.store import PackageStore
from landscape.package.reporter import find_reporter_command
from landscape.monitor.monitor import MonitorPlugin


class PackageMonitor(MonitorPlugin):

    run_interval = 1800

    def __init__(self, package_store_filename=None):
        super(PackageMonitor, self).__init__()
        if package_store_filename:
            self._package_store = PackageStore(package_store_filename)
        else:
            self._package_store = None
        self._reporter_command = find_reporter_command()

    def register(self, registry):
        self.config = registry.config
        super(PackageMonitor, self).register(registry)

        if not self._package_store:
            filename = os.path.join(registry.config.data_path,
                                    "package/database")
            self._package_store = PackageStore(filename)

        registry.reactor.call_on(("message", "package-ids"),
                                 self._enqueue_message_as_reporter_task)
        registry.reactor.call_on("resynchronize", self._resynchronize)
        self.call_on_accepted("packages", self.spawn_reporter)
        self.run()

    def _enqueue_message_as_reporter_task(self, message):
        self._package_store.add_task("reporter", message)
        self.spawn_reporter()

    def run(self):
        result = self.registry.broker.get_accepted_message_types()
        result.addCallback(self._got_message_types)
        return result

    def _got_message_types(self, message_types):
        if "packages" in message_types:
            self.spawn_reporter()

    def spawn_reporter(self):
        args = ["--quiet"]
        if self.config.config:
            args.extend(["-c", self.config.config])

        # path is set to None so that getProcessOutput does not
        # chdir to "." see bug #211373
        result = getProcessOutput(self._reporter_command,
                                  args=args, env=os.environ,
                                  errortoo=1,
                                  path=None)
        result.addCallback(self._got_reporter_output)
        return result

    def _got_reporter_output(self, output):
        if output:
            logging.warning("Package reporter output:\n%s" % output)

    def _resynchronize(self):
        """
        Remove all tasks *except* the resynchronize task.  This is
        because if we clear all tasks, then add the resynchronize,
        it's possible that the reporter may be running a task at this
        time and when it finishes, it will unknowningly remove the
        resynchronize task because sqlite resets its serial primary
        keys when you delete an entire table.  This problem is avoided
        by adding the task first and removing them all *except* the
        resynchronize task and not causing sqlite to reset the serial
        key.
        """
        task = self._package_store.add_task("reporter",
                                            {"type" : "resynchronize"})
        self._package_store.clear_tasks(except_tasks=(task,))
        
