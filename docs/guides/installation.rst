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
container access, or elevated privileges. Check the relevant fault guide and
run preflight before applying a fault. Installing the Python package alone
does not install system tools on remote targets. With ``auto_install=True`` on
``ChaosRunner``, preflight can install missing declared tools such as
``stress-ng``, ``tc`` (``iproute2``), and ``iptables`` using the target's package
manager. This is opt-in and requires package-installation privileges on that
target; it does not install a Docker daemon, enable systemd, or grant kernel
capabilities.

The following tools are needed on the **target**, not necessarily on the
machine running CJ. The listed packages are checked during fault preflight;
``auto_install=True`` lets CJ install missing declared packages where the
target has a supported package manager and installation privileges.

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
