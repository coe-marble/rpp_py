#!/bin/bash
# Run inside the CI container of rpp_py, with rpp_common, rpp_plugin_registrator, rpp_orchestrator,
# rpp_py, rpp_cli and rpp_testing installed (pip install --no-deps -e).
cd "$(dirname "$0")" && python reproduce.py
