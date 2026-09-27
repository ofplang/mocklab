# Entry point: `python -m automated_thermal_cycler_mock_lads --insecure`. Everything else is in lads_common.server.
from lads_common import run

from .server import SPEC, build

if __name__ == "__main__":
    run(SPEC, build)
