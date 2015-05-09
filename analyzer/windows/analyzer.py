# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import sys
import socket
import struct
import pkgutil
import logging
import hashlib
import xmlrpclib
import traceback
from ctypes import create_string_buffer, byref, c_int, sizeof
from threading import Lock, Thread
from datetime import datetime

from lib.api.process import Process
from lib.common.abstracts import Package, Auxiliary
from lib.common.constants import PATHS, PIPE, SHUTDOWN_MUTEX
from lib.common.defines import KERNEL32
from lib.common.defines import ERROR_MORE_DATA, ERROR_PIPE_CONNECTED
from lib.common.defines import PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE
from lib.common.defines import PIPE_READMODE_MESSAGE, PIPE_WAIT
from lib.common.defines import PIPE_UNLIMITED_INSTANCES, INVALID_HANDLE_VALUE
from lib.common.exceptions import CuckooError, CuckooPackageError
from lib.common.hashing import hash_file
from lib.common.results import upload_to_host
from lib.core.config import Config
from lib.core.packages import choose_package
from lib.core.privileges import grant_debug_privilege
from lib.core.startup import create_folders, init_logging
from modules import auxiliary

log = logging.getLogger()

BUFSIZE = 4096
PROCESS_LOCK = Lock()
DEFAULT_DLL = None

PID = os.getpid()
PPID = Process(pid=PID).get_parent_pid()

class Files(object):
    PROTECTED_NAMES = ()

    def __init__(self):
        self.files = []
        self.dumped = []

    def is_protected_filename(self, file_name):
        """Do we want to inject into a process with this name?"""
        return file_name.lower() in self.PROTECTED_NAMES

    def add_file(self, filepath):
        """Add filepath to the list of files."""
        if filepath not in self.files:
            log.info("Added new file to list with path: %s", filepath)
            self.files.append(filepath.lower())

    def dump_file(self, filepath):
        """Dump a file to the host."""
        if not os.path.isfile(filepath):
            log.warning("File at path \"%s\" does not exist, skip.", filepath)
            return False

        # Check whether we've already dumped this file - in that case skip it.
        sha256 = hash_file(hashlib.sha256, filepath)
        if sha256 in self.dumped:
            return

        filename = "%s_%s" % (sha256[:16], os.path.basename(filepath))
        upload_path = os.path.join("files", filename)

        try:
            upload_to_host(filepath, upload_path)
            self.dumped.append(sha256)
        except (IOError, socket.error) as e:
            log.error("Unable to upload dropped file at path \"%s\": %s",
                      filepath, e)

    def delete_file(self, filepath):
        """A file is about to removed and thus should be dumped right away."""
        self.dump_file(filepath)

        # Remove the filepath from the files list.
        if filepath.lower() in self.files:
            self.files.remove(filepath.lower())

    def move_file(self, oldfilepath, newfilepath):
        """A file will be moved - track this change."""
        if oldfilepath.lower() in self.files:
            # Replace the entry with the new filepath.
            index = self.files.index(oldfilepath.lower())
            self.files[index] = newfilepath.lower()

    def dump_files(self):
        """Dump all pending files."""
        for filepath in self.files:
            self.dump_file(filepath)

class ProcessList(object):
    def __init__(self):
        self.pids = []

    def add_pid(self, pid):
        """Add a process identifier to the process list."""
        log.info("Added new process to list with pid: %s", pid)
        self.pids.append(int(pid))

    def add_pids(self, pids):
        """Add one or more process identifiers to the process list."""
        if isinstance(pids, (tuple, list)):
            for pid in pids:
                self.add_pid(pid)
        else:
            self.add_pid(pids)

    def has_pid(self, pid):
        """Is this process identifier being tracked?"""
        return int(pid) in self.pids

    def remove_pid(self, pid):
        """Remove a process identifier from being tracked."""
        self.pids.remove(pid)

FILES = Files()
PROCESS_LIST = ProcessList()

class PipeHandler(Thread):
    """Pipe Handler.

    This class handles the notifications received through the Pipe Server and
    decides what to do with them.
    """

    def __init__(self, h_pipe):
        """@param h_pipe: PIPE to read."""
        Thread.__init__(self)
        self.h_pipe = h_pipe

    def _handle_debug(self, data):
        """Debug message from the monitor."""
        log.debug(data)

    def _handle_info(self, data):
        """Regular message from the monitor."""
        log.info(data)

    def _handle_critical(self, data):
        """Critical message from the monitor."""
        log.critical(data)

    def _handle_loaded(self, data):
        """The monitor has loaded into a particular process."""
        if not data or not data.isdigit():
            log.warning("Received loaded command with incorrect process "
                        "identifier, skipping it.")
            return

        PROCESS_LOCK.acquire()
        PROCESS_LIST.add_pid(int(data))
        PROCESS_LOCK.release()

        log.debug("Loaded monitor into process with pid %s", data)

    def _handle_getpids(self, data):
        """Return the process identifiers of the agent and its parent
        process."""
        return struct.pack("II", PID, PPID)

    def _inject_process(self, process_id, thread_id):
        """Helper function for injecting the monitor into a process."""
        # We acquire the process lock in order to prevent the analyzer to
        # terminate the analysis while we are operating on the new process.
        PROCESS_LOCK.acquire()

        # Set the current DLL to the default one provided at submission.
        dll = DEFAULT_DLL

        if process_id in (PID, PPID):
            log.warning("Received request to inject Cuckoo processes, "
                        "skipping it.")
            PROCESS_LOCK.release()
            return

        # We inject the process only if it's not being monitored already,
        # otherwise we would generated polluted logs (if it wouldn't crash
        # horribly to start with).
        if PROCESS_LIST.has_pid(process_id):
            log.warning("Received request to inject into process that we "
                        "are already monitoring, ignoring it.")
            # We're done operating on the processes list,
            # release the lock.
            PROCESS_LOCK.release()
            return

        # Open the process and inject the DLL. Hope it enjoys it.
        proc = Process(pid=process_id, tid=thread_id)

        filename = os.path.basename(proc.get_filepath())
        log.info("Announced process name: %s", filename)

        if not FILES.is_protected_filename(filename):
            # Add the new process ID to the list of monitored processes.
            PROCESS_LIST.add_pid(process_id)

            # We're done operating on the processes list,
            # release the lock. Let the injection do its thing.
            PROCESS_LOCK.release()

            log.debug("Injecting into process with pid %d", proc.pid)

            # If we have both pid and tid, then we can use APC to inject.
            if process_id and thread_id:
                proc.inject(dll, apc=True)
            else:
                proc.inject(dll, apc=False)

            log.info("Successfully injected process with pid %s", proc.pid)

    def _handle_process(self, data):
        """Request for injection into a process."""
        # Parse the process identifier.
        if not data or not data.isdigit():
            log.warning("Received PROCESS command from monitor with an "
                        "incorrect argument.")
            return

        return self._inject_process(int(data), None)

    def _handle_process2(self, data):
        """Request for injection into a process using APC."""
        # Parse the process and thread identifier.
        if not data or data.count(",") != 1:
            log.warning("Received PROCESS2 command from monitor with an "
                        "incorrect argument.")
            return

        pid, tid = data.split(",")
        if not pid.isdigit() or not tid.isdigit():
            log.warning("Received PROCESS2 command from monitor with an "
                        "incorrect argument.")
            return

        return self._inject_process(int(pid), int(tid))

    def _handle_file_new(self, data):
        """Notification of a new dropped file."""
        # Extract the file path and add it to the list.
        FILES.add_file(data.decode("utf8"))

    def _handle_file_del(self, data):
        """Notification of a file being removed - we have to dump it before
        it's being removed."""
        FILES.delete_file(data.decode("utf8"))

    def _handle_file_move(self, data):
        """A file is being moved - track these changes."""
        if "::" not in data:
            log.warning("Received FILE_MOVE command from monitor with an "
                        "incorrect argument.")
            return

        old_filepath, new_filepath = data.split("::", 1)
        FILES.move_file(old_filepath.decode("utf8"),
                        new_filepath.decode("utf8"))

    def run(self):
        """Run handler.
        @return: operation status.
        """
        data, response = "", "OK"

        # Read the data submitted to the Pipe Server.
        while True:
            bytes_read = c_int(0)

            buf = create_string_buffer(BUFSIZE)
            success = KERNEL32.ReadFile(self.h_pipe,
                                        buf,
                                        sizeof(buf),
                                        byref(bytes_read),
                                        None)

            data += buf.value

            if not success and KERNEL32.GetLastError() == ERROR_MORE_DATA:
                continue

            break

        if not data or ":" not in data:
            log.critical("Unknown command received from the monitor: %r",
                         data.strip())
        else:
            command, arguments = data.strip().split(":", 1)

            if not hasattr(self, "_handle_%s" % command.lower()):
                log.critical("Unknown command received from the monitor: %r",
                             data.strip())
            else:
                fn = getattr(self, "_handle_%s" % command.lower())
                response = fn(arguments) or ""

        KERNEL32.WriteFile(self.h_pipe,
                           create_string_buffer(response),
                           len(response),
                           byref(bytes_read),
                           None)

        KERNEL32.CloseHandle(self.h_pipe)
        return True

class PipeServer(Thread):
    """Cuckoo PIPE server.

    This Pipe Server receives notifications from the injected processes for
    new processes being spawned and for files being created or deleted.
    """

    def __init__(self, pipe_name=PIPE):
        """@param pipe_name: Cuckoo PIPE server name."""
        Thread.__init__(self)
        self.pipe_name = pipe_name
        self.do_run = True

    def stop(self):
        """Stop PIPE server."""
        self.do_run = False

    def run(self):
        """Create and run PIPE server.
        @return: operation status.
        """
        while self.do_run:
            # Create the Named Pipe.
            h_pipe = KERNEL32.CreateNamedPipeA(self.pipe_name,
                                               PIPE_ACCESS_DUPLEX,
                                               PIPE_TYPE_MESSAGE |
                                               PIPE_READMODE_MESSAGE |
                                               PIPE_WAIT,
                                               PIPE_UNLIMITED_INSTANCES,
                                               BUFSIZE,
                                               BUFSIZE,
                                               0,
                                               None)

            if h_pipe == INVALID_HANDLE_VALUE:
                return False

            # If we receive a connection to the pipe, we invoke the handler.
            if KERNEL32.ConnectNamedPipe(h_pipe, None) or KERNEL32.GetLastError() == ERROR_PIPE_CONNECTED:
                handler = PipeHandler(h_pipe)
                handler.daemon = True
                handler.start()
            else:
                KERNEL32.CloseHandle(h_pipe)

        return True

class Analyzer:
    """Cuckoo Windows Analyzer.

    This class handles the initialization and execution of the analysis
    procedure, including handling of the pipe server, the auxiliary modules and
    the analysis packages.
    """
    PIPE_SERVER_COUNT = 4

    def __init__(self):
        self.pipes = [None]*self.PIPE_SERVER_COUNT
        self.config = None
        self.target = None

    def prepare(self):
        """Prepare env for analysis."""
        global DEFAULT_DLL

        # Get SeDebugPrivilege for the Python process. It will be needed in
        # order to perform the injections.
        grant_debug_privilege()

        # Create the folders used for storing the results.
        create_folders()

        # Initialize logging.
        init_logging()

        # Parse the analysis configuration file generated by the agent.
        self.config = Config(cfg="analysis.conf")

        # Set virtual machine clock.
        clock = datetime.strptime(self.config.clock, "%Y%m%dT%H:%M:%S")
        # Setting date and time.
        # NOTE: Windows system has only localized commands with date format
        # following localization settings, so these commands for english date
        # format cannot work in other localizations.
        # In addition DATE and TIME commands are blocking if an incorrect
        # syntax is provided, so an echo trick is used to bypass the input
        # request and not block analysis.
        os.system("echo:|date {0}".format(clock.strftime("%m-%d-%y")))
        os.system("echo:|time {0}".format(clock.strftime("%H:%M:%S")))

        # Set the default DLL to be used by the PipeHandler.
        DEFAULT_DLL = self.config.get_options().get("dll")

        # Initialize and start the Pipe Servers. This is going to be used for
        # communicating with the injected and monitored processes.
        for x in xrange(self.PIPE_SERVER_COUNT):
            self.pipes[x] = PipeServer()
            self.pipes[x].daemon = True
            self.pipes[x].start()

        # We update the target according to its category. If it's a file, then
        # we store the path.
        if self.config.category == "file":
            self.target = os.path.join(os.environ["TEMP"] + os.sep,
                                       str(self.config.file_name))
        # If it's a URL, well.. we store the URL.
        else:
            self.target = self.config.target

    def complete(self):
        """End analysis."""
        # Stop the Pipe Servers.
        for x in xrange(self.PIPE_SERVER_COUNT):
            self.pipes[x].stop()

        # Dump all the notified files.
        FILES.dump_files()

        # Hell yeah.
        log.info("Analysis completed.")

    def run(self):
        """Run analysis.
        @return: operation status.
        """
        self.prepare()

        log.debug("Starting analyzer from: %s", os.getcwd())
        log.debug("Storing results at: %s", PATHS["root"])
        log.debug("Pipe server name: %s", PIPE)

        # If no analysis package was specified at submission, we try to select
        # one automatically.
        if not self.config.package:
            log.debug("No analysis package specified, trying to detect "
                      "it automagically.")

            # If the analysis target is a file, we choose the package according
            # to the file format.
            if self.config.category == "file":
                package = choose_package(self.config.file_type, self.config.file_name)
            # If it's an URL, we'll just use the default Internet Explorer
            # package.
            else:
                package = "ie"

            # If we weren't able to automatically determine the proper package,
            # we need to abort the analysis.
            if not package:
                raise CuckooError("No valid package available for file "
                                  "type: {0}".format(self.config.file_type))

            log.info("Automatically selected analysis package \"%s\"", package)
        # Otherwise just select the specified package.
        else:
            package = self.config.package

        # Generate the package path.
        package_name = "modules.packages.%s" % package

        # Try to import the analysis package.
        try:
            __import__(package_name, globals(), locals(), ["dummy"], -1)
        # If it fails, we need to abort the analysis.
        except ImportError:
            raise CuckooError("Unable to import package \"{0}\", does "
                              "not exist.".format(package_name))

        # Initialize the package parent abstract.
        Package()

        # Enumerate the abstract subclasses.
        try:
            package_class = Package.__subclasses__()[0]
        except IndexError as e:
            raise CuckooError("Unable to select package class "
                              "(package={0}): {1}".format(package_name, e))

        # Initialize the analysis package.
        pack = package_class(self.config.get_options())

        # Initialize Auxiliary modules
        Auxiliary()
        prefix = auxiliary.__name__ + "."
        for loader, name, ispkg in pkgutil.iter_modules(auxiliary.__path__, prefix):
            if ispkg:
                continue

            # Import the auxiliary module.
            try:
                __import__(name, globals(), locals(), ["dummy"], -1)
            except ImportError as e:
                log.warning("Unable to import the auxiliary module "
                            "\"%s\": %s", name, e)

        # Walk through the available auxiliary modules.
        aux_enabled, aux_avail = [], []
        for module in Auxiliary.__subclasses__():
            # Try to start the auxiliary module.
            try:
                aux = module(self.config.get_options())
                aux_avail.append(aux)
                aux.start()
            except (NotImplementedError, AttributeError):
                log.warning("Auxiliary module %s was not implemented",
                            aux.__class__.__name__)
                continue
            except Exception as e:
                log.warning("Cannot execute auxiliary module %s: %s",
                            aux.__class__.__name__, e)
                continue
            finally:
                log.debug("Started auxiliary module %s",
                          aux.__class__.__name__)
                aux_enabled.append(aux)

        # Start analysis package. If for any reason, the execution of the
        # analysis package fails, we have to abort the analysis.
        try:
            pids = pack.start(self.target)
        except NotImplementedError:
            raise CuckooError("The package \"{0}\" doesn't contain a run "
                              "function.".format(package_name))
        except CuckooPackageError as e:
            raise CuckooError("The package \"{0}\" start function raised an "
                              "error: {1}".format(package_name, e))
        except Exception as e:
            raise CuckooError("The package \"{0}\" start function encountered "
                              "an unhandled exception: "
                              "{1}".format(package_name, e))

        # If the analysis package returned a list of process identifiers, we
        # add them to the list of monitored processes and enable the process monitor.
        if pids:
            PROCESS_LIST.add_pids(pids)
            pid_check = True

        # If the package didn't return any process ID (for example in the case
        # where the package isn't enabling any behavioral analysis), we don't
        # enable the process monitor.
        else:
            log.info("No process IDs returned by the package, running "
                     "for the full timeout.")
            pid_check = False

        # Check in the options if the user toggled the timeout enforce. If so,
        # we need to override pid_check and disable process monitor.
        if self.config.enforce_timeout:
            log.info("Enabled timeout enforce, running for the full timeout.")
            pid_check = False

        time_counter = 0

        while True:
            time_counter += 1
            if time_counter == int(self.config.timeout):
                log.info("Analysis timeout hit, terminating analysis.")
                break

            # If the process lock is locked, it means that something is
            # operating on the list of monitored processes. Therefore we
            # cannot proceed with the checks until the lock is released.
            if PROCESS_LOCK.locked():
                KERNEL32.Sleep(1000)
                continue

            try:
                # If the process monitor is enabled we start checking whether
                # the monitored processes are still alive.
                if pid_check:
                    for pid in PROCESS_LIST.pids:
                        if not Process(pid=pid).is_alive():
                            log.info("Process with pid %s has terminated", pid)
                            PROCESS_LIST.remove_pid(pid)

                    # If none of the monitored processes are still alive, we
                    # can terminate the analysis.
                    if not PROCESS_LIST.pids:
                        log.info("Process list is empty, "
                                 "terminating analysis.")
                        break

                    # Update the list of monitored processes available to the
                    # analysis package. It could be used for internal
                    # operations within the module.
                    pack.set_pids(PROCESS_LIST.pids)

                try:
                    # The analysis packages are provided with a function that
                    # is executed at every loop's iteration. If such function
                    # returns False, it means that it requested the analysis
                    # to be terminate.
                    if not pack.check():
                        log.info("The analysis package requested the "
                                 "termination of the analysis.")
                        break

                # If the check() function of the package raised some exception
                # we don't care, we can still proceed with the analysis but we
                # throw a warning.
                except Exception as e:
                    log.warning("The package \"%s\" check function raised "
                                "an exception: %s", package_name, e)
            finally:
                # Zzz.
                KERNEL32.Sleep(1000)

        # Create the shutdown mutex.
        KERNEL32.CreateMutexA(None, False, SHUTDOWN_MUTEX)

        try:
            # Before shutting down the analysis, the package can perform some
            # final operations through the finish() function.
            pack.finish()
        except Exception as e:
            log.warning("The package \"%s\" finish function raised an "
                        "exception: %s", package_name, e)

        try:
            # Upload files the package created to package_files in the results folder
            package_files = pack.package_files()
            if package_files is not None:
                for path, name in package_files:
                    upload_to_host(path, os.path.join("package_files", name))
        except Exception as e:
            log.warning("The package \"%s\" package_files function raised an "
                        "exception: %s", package_name, e)

        # Terminate the Auxiliary modules.
        for aux in aux_enabled:
            try:
                aux.stop()
            except (NotImplementedError, AttributeError):
                continue
            except Exception as e:
                log.warning("Cannot terminate auxiliary module %s: %s",
                            aux.__class__.__name__, e)

        if self.config.terminate_processes:
            # Try to terminate remaining active processes. We do this to make sure
            # that we clean up remaining open handles (sockets, files, etc.).
            log.info("Terminating remaining processes before shutdown.")

            for pid in PROCESS_LIST.pids:
                proc = Process(pid=pid)
                if proc.is_alive():
                    try:
                        proc.terminate()
                    except:
                        continue

        # Run the finish callback of every available Auxiliary module.
        for aux in aux_avail:
            try:
                aux.finish()
            except (NotImplementedError, AttributeError):
                continue
            except Exception as e:
                log.warning("Exception running finish callback of auxiliary "
                            "module %s: %s", aux.__class__.__name__, e)

        # Let's invoke the completion procedure.
        self.complete()

        return True

if __name__ == "__main__":
    success = False
    error = ""

    try:
        # Initialize the main analyzer class.
        analyzer = Analyzer()

        # Run it and wait for the response.
        success = analyzer.run()

    # This is not likely to happen.
    except KeyboardInterrupt:
        error = "Keyboard Interrupt"

    # If the analysis process encountered a critical error, it will raise a
    # CuckooError exception, which will force the termination of the analysis.
    # Notify the agent of the failure. Also catch unexpected exceptions.
    except Exception as e:
        # Store the error.
        error_exc = traceback.format_exc()
        error = str(e)

        # Just to be paranoid.
        if len(log.handlers):
            log.exception(error_exc)
        else:
            sys.stderr.write("{0}\n".format(error_exc))

    # Once the analysis is completed or terminated for any reason, we report
    # back to the agent, notifying that it can report back to the host.
    finally:
        # Establish connection with the agent XMLRPC server.
        server = xmlrpclib.Server("http://127.0.0.1:8000")
        server.complete(success, error, PATHS["root"])
