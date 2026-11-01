#!/usr/bin/with-contenv bashio
# Starts the collector. SUPERVISOR_TOKEN is provided by the Supervisor.
bashio::log.info "Starting YVZ Heating Insights collector"
exec python3 -u /app/collector.py
