# Setup

## Requirements

- Windows, Linux, or macOS for the server
- Python 3.10 or later, subject to the repository lockfile or requirements
- Raspberry Pi with I2C enabled for the physical node
- MAX30102 PPG sensor
- MLX90614 temperature sensor
- Local network access between the Pi and server

## Clone

```bash
git clone https://github.com/YOUR_USERNAME/EdgeER.git
cd EdgeER
```

## Server

```powershell
cd server
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python server.py
```

The dashboard is normally exposed by the FastAPI application on port 8000. Use the server console output as the authoritative address.

## Raspberry Pi edge node

```bash
cd edge-node
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Configure the server URL and device identity through the repository's configuration mechanism. Avoid committing personal IP addresses or credentials as mandatory defaults.

## Simulator

Run the simulator entry point included in the repository, commonly:

```powershell
python simulator\simulate_nodes.py
```

The simulator supports multiple virtual devices and reproducible fault scenarios.

## Hardware notes

- Enable I2C on Raspberry Pi OS.
- Confirm sensor addresses with `i2cdetect` where available.
- Install Adafruit Blinka and the MLX90614 library if required by the selected sensor driver.
- The MAX30102 must have one continuous hardware reader in normal operation.
