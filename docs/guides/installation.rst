Installation and verification
=============================

Chaos Jungle requires Python 3.9 or newer. Use a virtual environment so the
package and its dependencies do not change the system Python installation.

Install the current source
--------------------------

Until a PyPI release is available, contributors with repository access can
install the current source from the Pegasus repository:

.. code-block:: bash

   python3 --version  # confirm Python 3.9 or newer
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install git+https://github.com/pegasus-isi/Chaos-jungle.git@main

The repository currently requires access; a public PyPI installation is not
yet available. A checkout of the same revision can instead be installed with
``python -m pip install -e .`` from its root directory.

On Windows, activate the environment with ``.venv\Scripts\activate``. If
``python3`` selects an older interpreter, use the command for a Python 3.9+
installation when creating the environment.

For development from a local checkout, replace the last command with
``python -m pip install -e ".[dev]"``. After the first PyPI release, users can
install the published package with ``python -m pip install chaos-jungle``.

Verify the installation
-----------------------

.. code-block:: bash

   python -c "import chaos_jungle; print(chaos_jungle.__version__)"
   chaos-jungle --help

These checks verify that Python can import the package and that its CLI entry
point is installed. They do not test fault delivery. To run the core tests in
a development checkout, use ``python -m pytest tests/ -q --strict-markers``.

Target requirements
-------------------

In-process HTTP interception can run without Linux privileges. Infrastructure
faults act on a target machine or container and may require Linux utilities,
container access, or elevated privileges. Installing the Python package alone
does not install system tools on local or remote targets. Before an experiment,
use ``chaos-jungle deps check`` to inspect the commands declared by a fault:

.. code-block:: bash

   chaos-jungle deps check --fault CPUStress
   chaos-jungle deps check --fault NetworkDelay --target ssh://user@worker1
   chaos-jungle deps check --all

``check`` is read-only and exits nonzero when a selected fault has missing
requirements. Fault names are built-in class names; ``cpu``, ``memory``, and
``io`` are shortcuts. Repeat ``--fault`` to check several faults. ``--all``
checks the mapped system commands declared across built-in faults, while
listing requirements that need separate setup.

To install only the missing mapped system packages, run:

.. code-block:: bash

   chaos-jungle deps install --fault CPUStress
   chaos-jungle deps install --fault NetworkDelay --target ssh://user@worker1
   chaos-jungle deps install --all

CJ shows the planned packages and asks for confirmation. Use ``--yes`` only
in trusted automation. Installation requires package-manager privileges on
the selected target; CJ uses APT on Debian/Ubuntu and DNF or YUM on
Fedora/RHEL-family systems. For example, ``NetworkDelay`` needs ``iproute2``
on Debian and ``iproute`` on RPM systems; ``ProcessKill`` needs ``procps``
and ``procps-ng``, respectively. Prefer ``--fault`` to avoid installing tools
for unrelated experiments. This command does not install Docker or start its
daemon, provision systemd, configure kernel/BPF capabilities, or install
target-side Python packages such as ``python-crontab``. A fault-specific
``install`` exits nonzero if such manual requirements remain; ``--all``
reports them outside its package-installation scope. Check the relevant fault
guide before applying a fault.

``ChaosRunner(..., auto_install=True)`` is a separate opt-in path that checks
and installs declared dependencies during fault preflight. It also requires
installation privileges and does not satisfy runtime or privilege requirements.

The following tools are needed on the **target**, not necessarily on the
machine running CJ. Fault preflight checks the declared packages; the CLI
commands above allow inspection and opt-in installation before the run.

.. list-table:: Target-side dependencies for command-based faults
   :header-rows: 1
   :widths: 28 29 43

   * - Faults
     - Required tool or package
     - Additional requirement
   * - ``CPUStress``, ``MemoryStress``, ``IOStress``
     - ``stress-ng``
     - Set a safe resource limit for the target workload.
   * - Network delay, loss, corruption, duplication, bandwidth, and reordering
     - ``tc`` (``iproute2``)
     - Network-administration privilege on the target.
   * - ``NetworkReset``, ``NetworkPartition``
     - ``iptables``
     - Network-administration privilege on the target.
   * - ``DiskFull``, ``InodeFull``
     - ``dd`` (``coreutils``)
     - Write permission on the selected path.
   * - ``ProcessKill``
     - ``pgrep`` / ``pkill`` (``procps``)
     - Permission to signal the selected process.
   * - ``StorageCorrupt``
     - ``python3``, ``e2fsprogs``, ``inotify-tools``, ``coreutils``, and
       ``python-crontab`` (target-side Python package)
     - Access to the selected files, ``pip3`` to install ``python-crontab``,
       and a working cron scheduler; CJ does not provision the scheduler.
   * - ``RedisStateCorrupt``, ``PostgresStateCorrupt``
     - ``redis-cli`` (``redis-tools``), ``psql`` (``postgresql-client``),
       respectively
     - Reachable service and valid credentials.
   * - ``ServiceFault``
     - ``systemctl``
     - A running systemd service manager; CJ cannot install or start one.
   * - ``ContainerKill``
     - Docker CLI
     - Access to a running Docker daemon; installing the CLI is not enough.

``SilentNetworkCorrupt`` has additional Linux BPF/BCC and kernel requirements;
see :doc:`network`. Fault-specific guides describe further target constraints.

For a local example, continue with :doc:`../quickstart`. For the GitLab build,
distribution checks, and PyPI release procedure, see :doc:`../publishing`.
