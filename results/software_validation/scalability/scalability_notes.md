# Scalability notes

TestClient provided bounded in-process load, not an external network saturation test. The normal load is measured here; packet fault variants are exercised by the packet-validation matrix, disconnect/reconnect faults by the reconnection group, and an exact duplicate probe is recorded separately in failure_events.csv. CPU is normalized process CPU time; memory is Windows working set. 
