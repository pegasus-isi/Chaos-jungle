"""Short CPU-bound workload executed inside the disposable resource target."""

import json
import time


def main() -> None:
    started = time.perf_counter()
    deadline = started + 0.5
    iterations = 0
    while time.perf_counter() < deadline:
        iterations += 1
    elapsed = time.perf_counter() - started
    print(json.dumps({"duration_s": elapsed, "throughput_ops_per_s": iterations / elapsed}))


if __name__ == "__main__":
    main()
