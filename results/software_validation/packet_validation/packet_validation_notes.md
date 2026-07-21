# Packet validation notes

Unknown devices are intentionally auto-registered for simulator compatibility. Sequence monotonicity is not enforced; only exact `(device_id, packet_sequence, device_send_time)` duplicates are rejected. Results use an isolated temporary database.
