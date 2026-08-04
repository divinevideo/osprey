#!/bin/bash
set -e

echo "Creating Bigtable instance and tables"

export BIGTABLE_EMULATOR_HOST=bigtable:8361

echo "Creating Bigtable instance"
cbt -project=osprey-dev -instance=osprey-bigtable createinstance osprey-bigtable 'Osprey Development Instance' osprey-bigtable-cluster us-central1-a 1 SSD || true

echo "Creating stored_execution_result table"
cbt -project=osprey-dev -instance=osprey-bigtable createtable stored_execution_result || true
cbt -project=osprey-dev -instance=osprey-bigtable createfamily stored_execution_result execution_result || true

echo "Bigtable initialization complete"
