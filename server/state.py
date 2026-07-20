latest_packets: list[dict] = []
patients: dict[str, dict] = {}
patient_events: dict[str, list[dict]] = {}
encounters: dict[str, dict] = {}
active_encounters: dict[str, str] = {}
devices: dict[str, dict] = {}

priority_patients: set[str] = set()
recheck_due_at: dict[str, float] = {}

patient_admission_times: dict[str, str] = {}
patient_dismissal_times: dict[str, str] = {}
dismissed_patients: set[str] = set()

patient_profiles: dict[str, dict] = {}
