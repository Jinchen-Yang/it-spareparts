#!/bin/bash
# 在隔离恢复库上执行只读 SQL：scripts/q.sh "<sql>"
exec docker exec -i maint-survey-pg-20261030 psql -U survey -d itsp -At -F"|" -c "$1"
