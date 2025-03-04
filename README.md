# qbitban

qbitban is a Python-based command-line tool aimed at optimizing [qBittorrent](https://github.com/qbittorrent/qBittorrent) upload performance. It connects to the qBittorrent API, monitors active torrents, tracks peer upload speeds, and automatically bans peers whose speeds fall below a specified threshold. This helps keeping uploads slots free while improving speed and data transfers. It also handles exceptions for torrents that don't have enough seeders or have specific tags.

## Project Structure

- `qbitban.py` – Main Python application containing the core logic.
- `qbitban.json` – Sample configuration file.
- `install.sh` – Shell script to install and build the project.
- `requirements.txt` – Python dependencies.
- `qbitban` - Service script for FreeBSD rc.d system.

## Requirements

- Python 3.7+
- External libraries listed in `requirements.txt`:
  - `aiohttp`
  - `cachetools`
- PyInstaller for building the executable.

## Installation

Note: By default, it's made to work on FreeBSD. It can be adapted to work on other systems as well, if needed.

Run the provided installation script to set up the environment and build the application. For example, open your terminal and execute:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/srevn/qbitban/refs/heads/main/install.sh)"
```

The script will:
- Clone the repository.
- Set up a Python virtual environment.
- Install required Python packages.
- Build the application using PyInstaller.
- Copy the necessary executables and configuration files to the appropriate system locations.
- Clean up after completion.

## Configuration

Update the configuration file `qbitban.json` with your settings. The configuration options include:

- `url`: URL of the qBittorrent instance.
- `username`: qBittorrent username.
- `password`: qBittorrent password.
- `upspeed_threshold`: Upload speed threshold to trigger banning. (in bps)
- `upspeed_samples`: Number of samples for speed analysis.
- `upspeed_interval`: Time interval (in seconds) between speed samples.
- `min_seeders`: Minimum number of seeders to bypass monitoring.
- `excluded_tags`: Tags to bypass monitoring. (`[""]` for none)
- `check_interval`: Interval (in seconds) for fetching new active torrents data.
- `clear_exception`: Interval after which cached exceptions for tracked data are cleared. (excluded peers or torrents)
- `clear_banned_ips`: Boolean to indicate if banned IPs should be cleared. (at startup and periodic)
- `clear_interval`: Interval (in seconds) for periodic purge of banned IPs from qBittorrent. (`0` to disable)
- `log_file_path`: Path for the log file.

## Usage

### Running from the Command Line

You can run the application by specifying the configuration file. For example:

```sh
python qbitban.py --config /path/to/qbitban.json
```

### Running as executable on FreeBSD

The installer will add a rc.d script for managing with FreeBSD. Check the script for possible options.

You can run with `service qbitban start` if properly configured.

## Logging

Logs are written to the file specified in `log_file_path` in your configuration as well as output to the console if ran from the command line.

Note: By default, only relevent information gets written in the log file (`INFO` level), while console stream will output everything. (`DEBUG` level)
