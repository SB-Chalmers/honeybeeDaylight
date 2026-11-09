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


# One process per physical core for both queues. EnergyPlus is CPU-bound, and
# so is a daylight job: profiled on the Precision 7540 (Foton 0.3.6), about 99%
# of a preview job is CPU work (rebuilding the Honeybee model, checking and
# meshing it) and about 1% is GPU tracing, so the GPU is nearly idle with two
# processes and more processes mean more jobs in flight. Without a GPU, Foton
# traces on the CPU and one process is enough.
def concurrency(queue: str) -> int:
    override = os.environ.get(f'{queue.upper()}_CONCURRENCY', '').strip()
    if override:
        return max(1, int(override))
    if queue == 'daylight' and not gpu_count():
        return 1
    return physical_cores()


if __name__ == '__main__':
    print(concurrency(sys.argv[1]))
