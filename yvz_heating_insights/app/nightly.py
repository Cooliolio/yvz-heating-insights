"""Extension point for later versions (daily files, model fitting).

Not part of v0.1: this stub only logs. It must stay read-only against HA
and may only read the monthly CSV files written by collector.py.
"""
import logging

log = logging.getLogger("nightly")


def main():
    log.info("nightly: not implemented")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
