"""Pick worker concurrency from the hardware, so no one has to tune it by hand.

    python -m worker.concurrency energy     # prints the number of EnergyPlus processes
    python -m worker.concurrency daylight   # prints the number of Foton processes

ENERGY_CONCURRENCY or DAYLIGHT_CONCURRENCY in .env override the choice.
"""

from __future__ import annotations

import glob
import os
import sys


def physical_cores() -> int:
    """Physical cores this container may use (hyperthreads counted once)."""
    allowed = os.sched_getaffinity(0)
    cores = set()
    for path in glob.glob('/sys/devices/system/cpu/cpu[0-9]*/topology/core_cpus_list'):
        cpu = int(path.split('/')[5][3:])
        if cpu in allowed:
            with open(path) as f:
                cores.add(f.read().strip())
    count = len(cores) or max(1, len(allowed) // 2)
    # A CPU limit (docker --cpus) shows up as a cgroup quota, not in the affinity set.
    try:
        quota, period = open('/sys/fs/cgroup/cpu.max').read().split()
        if quota != 'max':
            count = min(count, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return count


def gpu_count() -> int:
    """NVIDIA GPUs passed into this container."""
    return len(glob.glob('/dev/nvidia[0-9]*'))


# Chosen from a sweep on the Precision 7540 (8 cores, 1 GPU) under five load
# patterns, from 4 clients to a 24-client class mix: one EnergyPlus process per
# physical core, and two Foton processes per GPU (one prepares the next model
# on the CPU while the other traces). More energy processes than cores add
# waiting, not throughput.
def concurrency(queue: str) -> int:
    override = os.environ.get(f'{queue.upper()}_CONCURRENCY', '').strip()
    if override:
        return max(1, int(override))
    if queue == 'energy':
        return physical_cores()
    return 2 * gpu_count() or 1


if __name__ == '__main__':
    print(concurrency(sys.argv[1]))
